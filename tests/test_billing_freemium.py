from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import stripe
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    APIToken,
    BillingProfile,
    Conversation,
    Domain,
    StripeWebhookEvent,
    User,
)
from inbox.services.billing import create_checkout_url, process_event
from inbox.services.domains import create_domain
from inbox.services.entitlements import can_manage_domain, for_user


@pytest.fixture
def free_owner(db) -> User:
    return User.objects.create_user(
        email="free@example.com",
        password="Correct-Horse-Battery-789",
        email_verified_at=timezone.now(),
        is_active=True,
    )


def make_domain(owner: User, hostname: str) -> Domain:
    return Domain.objects.create(
        owner=owner,
        hostname=hostname,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.READY,
        inbound_ready=True,
        claim_expires_at=timezone.now() + timedelta(days=3),
    )


@pytest.mark.django_db
@override_settings(MAX_DOMAINS_PER_USER=20)
@pytest.mark.parametrize(
    ("status", "is_pro"),
    [
        (BillingProfile.SubscriptionStatus.ACTIVE, True),
        (BillingProfile.SubscriptionStatus.TRIALING, True),
        (BillingProfile.SubscriptionStatus.PAST_DUE, True),
        (BillingProfile.SubscriptionStatus.CANCELED, False),
        (BillingProfile.SubscriptionStatus.UNPAID, False),
        (BillingProfile.SubscriptionStatus.INCOMPLETE, False),
    ],
)
def test_subscription_status_controls_entitlements(free_owner, status, is_pro):
    profile = BillingProfile.objects.create(
        user=free_owner,
        subscription_status=status,
        subscription_plan="pro",
    )

    entitlements = for_user(free_owner)

    assert profile.is_pro is is_pro
    assert entitlements.is_pro is is_pro
    assert entitlements.domain_limit == (20 if is_pro else 1)


@pytest.mark.django_db
def test_free_account_can_manage_only_oldest_domain(free_owner):
    first = make_domain(free_owner, "first.example")
    second = make_domain(free_owner, "second.example")

    assert can_manage_domain(free_owner, first)
    assert not can_manage_domain(free_owner, second)


@pytest.mark.django_db
def test_free_domain_limit_is_enforced(monkeypatch, free_owner):
    make_domain(free_owner, "first.example")
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])

    with pytest.raises(ValidationError, match="Active domain capacity is 1"):
        create_domain(
            owner=free_owner,
            hostname="second.example",
            setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        )


@pytest.mark.django_db
def test_free_domain_connect_action_redirects_to_upgrade_only_after_click(client, free_owner):
    make_domain(free_owner, "first.example")
    client.force_login(free_owner)

    domains = client.get(reverse("domains"))

    assert domains.status_code == 200
    assert b">Connect domain</a>" in domains.content
    assert b"Upgrade to connect domain" not in domains.content

    connect = client.get(reverse("domain_create"))

    assert connect.status_code == 302
    assert connect.url == reverse("billing")


@pytest.mark.django_db
def test_free_api_token_uses_existing_domain_and_obeys_one_domain_limit(
    client, monkeypatch, free_owner
):
    domain = make_domain(free_owner, "api-free.example")
    _, raw = APIToken.issue(owner=free_owner)
    headers = {"Authorization": f"Bearer {raw}"}

    response = client.get(f"/api/v1/domains/{domain.id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == str(domain.id)

    outbound = client.post(
        f"/api/v1/domains/{domain.id}/outbound/enable",
        data={},
        content_type="application/json",
        headers=headers,
    )

    assert outbound.status_code == 202
    assert outbound.json()["outbound_status"] == Domain.OutboundStatus.PROVISIONING
    assert outbound.json()["started"] is True

    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    second = client.post(
        "/api/v1/domains",
        data={"hostname": "second-api-free.example", "setup_mode": "DIRECT_MX"},
        content_type="application/json",
        headers=headers,
    )

    assert second.status_code == 409
    assert second.json() == {
        "code": "capacity_reached",
        "message": "Active domain capacity is 1; current usage is 1.",
        "fields": {},
        "request_id": second.json()["request_id"],
        "resource": "active_domains",
        "used": 1,
        "limit": 1,
        "remaining": 0,
        "reset_at": None,
        "retryable": False,
    }


@pytest.mark.django_db
def test_domain_provision_rate_limit_returns_retry_timing(
    client, monkeypatch, settings, owner, domain
):
    settings.DOMAIN_PROVISION_RATE_LIMIT = 1
    settings.DOMAIN_PROVISION_RATE_WINDOW_SECONDS = 600
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    _, raw = APIToken.issue(owner=owner)

    response = client.post(
        "/api/v1/domains",
        data={"hostname": "rate-limited.example", "setup_mode": "DIRECT_MX"},
        content_type="application/json",
        headers={"Authorization": f"Bearer {raw}"},
    )

    assert response.status_code == 429
    payload = response.json()
    assert payload["code"] == "rate_limited"
    assert payload["resource"] == "domain_provisioning"
    assert payload["retryable"] is True
    assert payload["retry_after_seconds"] > 0
    assert payload["next_allowed_at"]
    assert response.headers["Retry-After"] == str(payload["retry_after_seconds"])
    assert payload["request_id"]
    assert "upgrade" not in response.content.decode().casefold()


@pytest.mark.django_db
def test_over_capacity_domain_is_readable_but_mutations_are_guarded_and_disable_is_allowed(
    client, free_owner
):
    primary = make_domain(free_owner, "primary-grace.example")
    read_only = make_domain(free_owner, "read-only-grace.example")
    _, raw = APIToken.issue(owner=free_owner)
    headers = {"Authorization": f"Bearer {raw}"}

    detail = client.get(f"/api/v1/domains/{read_only.id}", headers=headers)
    blocked = client.post(
        f"/api/v1/domains/{read_only.id}/check",
        data={},
        content_type="application/json",
        headers=headers,
    )
    primary_check = client.post(
        f"/api/v1/domains/{primary.id}/check",
        data={},
        content_type="application/json",
        headers=headers,
    )
    disabled = client.post(
        f"/api/v1/domains/{read_only.id}/disable",
        data={},
        content_type="application/json",
        headers=headers,
    )

    assert detail.status_code == 200
    assert blocked.status_code == 403
    assert blocked.json() == {
        "code": "domain_read_only",
        "message": (
            "This domain is read-only while the account exceeds its active-domain capacity."
        ),
        "fields": {},
        "request_id": blocked.json()["request_id"],
        "resource": "active_domains",
        "used": 2,
        "limit": 1,
        "remaining": 0,
        "reset_at": None,
        "retryable": False,
    }
    assert primary_check.status_code == 409
    assert primary_check.json()["code"] == "dns_instructions_not_ready"
    assert disabled.status_code == 202
    read_only.refresh_from_db()
    assert read_only.status == Domain.Status.DISABLED


@pytest.mark.django_db
def test_free_account_can_create_personal_api_token(client, free_owner):
    client.force_login(free_owner)

    response = client.post(reverse("api_tokens"), {})

    assert response.status_code == 302
    assert response.url == reverse("api_tokens")
    assert APIToken.objects.filter(owner=free_owner, revoked_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_free_conversation_has_no_server_side_draft_action(client, free_owner):
    domain = make_domain(free_owner, "draft-upgrade.example")
    conversation = Conversation.objects.create(
        domain=domain,
        subject="Upgrade from draft action",
        normalized_subject="upgrade from draft action",
        first_message_at=timezone.now(),
        last_message_at=timezone.now(),
    )
    client.force_login(free_owner)
    session = client.session
    session["domain_id"] = str(domain.id)
    session.save()

    response = client.get(reverse("conversation_detail", args=[conversation.id]))

    assert response.status_code == 200
    assert b"Free inbox mode" not in response.content
    assert b"AI drafts and outbound sending require" not in response.content
    assert b"No agent-authored reply yet" in response.content
    assert b"Generate review draft" not in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("domain_status", "inbound_ready", "outbound_status", "expected_action"),
    [
        (Domain.Status.PENDING_TEST, False, Domain.OutboundStatus.DISABLED, None),
        (Domain.Status.READY, True, Domain.OutboundStatus.DISABLED, b"Enable sending"),
        (Domain.Status.READY, True, Domain.OutboundStatus.ERROR, b"Retry sending setup"),
    ],
)
def test_free_domain_detail_exposes_outbound_setup_without_upgrade_gate(
    client, free_owner, domain_status, inbound_ready, outbound_status, expected_action
):
    domain = make_domain(free_owner, "outbound-upgrade.example")
    domain.status = domain_status
    domain.inbound_ready = inbound_ready
    domain.outbound_status = outbound_status
    domain.save(update_fields=("status", "inbound_ready", "outbound_status", "updated_at"))
    client.force_login(free_owner)

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Upgrade to set up outbound sending" not in response.content
    if expected_action is None:
        assert b"Enable sending" not in response.content
        assert b"Retry sending setup" not in response.content
        assert reverse("domain_enable_outbound", args=[domain.id]).encode() not in response.content
    else:
        assert expected_action in response.content
        assert reverse("domain_enable_outbound", args=[domain.id]).encode() in response.content


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="",
    STRIPE_WEBHOOK_SECRET="",
    STRIPE_PRO_UNIT_AMOUNT=499,
    STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT=999,
    STRIPE_PRO_CURRENCY="usd",
)
def test_free_billing_page_renders_upgrade_state(client, free_owner):
    make_domain(free_owner, "billing-free.example")
    client.force_login(free_owner)

    response = client.get(reverse("billing"))

    assert response.status_code == 200
    assert b"Current plan" in response.content
    assert b"Free" in response.content
    assert response.context["billing_configured"] is False
    assert response.context["price"] is None
    assert b"Online upgrades are temporarily unavailable" in response.content
    assert b"Limited-time price" not in response.content
    assert b"Regular price" not in response.content
    assert b"USD 4.99" not in response.content
    assert b"Upgrade to Pro" not in response.content
    assert reverse("billing_checkout").encode() not in response.content
    assert b"Billed monthly through Stripe" not in response.content


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="sk_test_example",
    STRIPE_WEBHOOK_SECRET="whsec_example",
    STRIPE_PRO_UNIT_AMOUNT=499,
    STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT=999,
    STRIPE_PRO_CURRENCY="usd",
    MAX_DOMAINS_PER_USER=20,
)
def test_free_billing_page_renders_limited_time_pro_offer(client, free_owner):
    make_domain(free_owner, "billing-offer.example")
    client.force_login(free_owner)

    response = client.get(reverse("billing"))

    assert response.status_code == 200
    assert response.context["billing_configured"] is True
    assert response.context["pro_domain_limit"] == 20
    assert response.context["price"] == {
        "unit_amount": 499,
        "amount": "4.99",
        "compare_at_unit_amount": 999,
        "compare_at_amount": "9.99",
        "currency": "USD",
        "interval": "month",
        "is_promotional": True,
        "product_name": "Operational Inbox Pro",
    }
    assert b"Limited-time price" in response.content
    assert b"Regular price" in response.content
    assert b"<del>USD 9.99</del>" in response.content
    assert b"USD 4.99" in response.content
    assert b"Up to 20 managed domains" in response.content
    assert b"5,000 user-requested replies" in response.content
    assert b"Custom retention &amp; server-side AI classification" in response.content
    assert b"API &amp; MCP access</span><span>Yes</span><span>Yes" in response.content
    assert b"Agent-authored drafts</span><span>Yes</span><span>Yes" in response.content
    assert f'method="post" action="{reverse("billing_checkout")}"'.encode() in response.content
    assert "Upgrade to Pro Scale · USD 4.99/month".encode() in response.content
    assert b"Billed monthly through Stripe" in response.content


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="sk_test_example",
    STRIPE_WEBHOOK_SECRET="whsec_example",
    STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT=999,
)
@pytest.mark.parametrize(
    ("unit_amount", "currency", "display_amount"),
    [
        (599, "usd", b"USD 5.99"),
        (499, "eur", b"EUR 4.99"),
        (2900, "usd", b"USD 29.00"),
    ],
)
def test_billing_page_does_not_claim_promotion_for_other_prices(
    client, free_owner, settings, unit_amount, currency, display_amount
):
    settings.STRIPE_PRO_UNIT_AMOUNT = unit_amount
    settings.STRIPE_PRO_CURRENCY = currency
    make_domain(free_owner, "billing-standard-price.example")
    client.force_login(free_owner)

    response = client.get(reverse("billing"))

    assert response.status_code == 200
    assert response.context["price"]["is_promotional"] is False
    assert response.context["price"]["compare_at_unit_amount"] is None
    assert response.context["price"]["compare_at_amount"] is None
    assert b"Limited-time price" not in response.content
    assert b"Regular price" not in response.content
    assert b"<del>" not in response.content
    assert b"Pro Scale price" in response.content
    assert display_amount in response.content
    assert b"Upgrade to Pro" in response.content
    assert display_amount + b"/month" in response.content


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="sk_test_example",
    STRIPE_WEBHOOK_SECRET="whsec_example",
    STRIPE_PRO_UNIT_AMOUNT=499,
    STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT=999,
    STRIPE_PRO_CURRENCY="usd",
)
def test_pro_billing_page_keeps_subscription_management_state(client, free_owner):
    make_domain(free_owner, "billing-pro.example")
    BillingProfile.objects.create(
        user=free_owner,
        subscription_status=BillingProfile.SubscriptionStatus.ACTIVE,
        subscription_plan="pro",
    )
    client.force_login(free_owner)

    response = client.get(reverse("billing"))

    assert response.status_code == 200
    assert response.context["plan_entitlements"].is_pro is True
    assert b"Manage subscription" in response.content
    assert f'method="post" action="{reverse("billing_portal")}"'.encode() in response.content
    assert reverse("billing_checkout").encode() not in response.content
    assert b"Limited-time price" not in response.content
    assert b"Regular price" not in response.content
    assert b"<del>" not in response.content
    assert b"USD 4.99" not in response.content
    assert b"Upgrade to Pro" not in response.content
    assert b"Billed monthly through Stripe" not in response.content


@pytest.mark.django_db
def test_extra_free_domain_is_read_only_but_preserved(client, free_owner):
    make_domain(free_owner, "primary.example")
    extra = make_domain(free_owner, "extra.example")
    conversation = Conversation.objects.create(
        domain=extra,
        subject="Read only",
        normalized_subject="read only",
        first_message_at=timezone.now(),
        last_message_at=timezone.now(),
    )
    client.force_login(free_owner)
    session = client.session
    session["domain_id"] = str(extra.id)
    session.save()

    detail = client.get(reverse("conversation_detail", args=[conversation.id]))
    response = client.post(
        reverse("conversation_tag", args=[conversation.id]),
        {"operation": "add", "tag": "agent-reviewed"},
    )

    conversation.refresh_from_db()
    assert detail.status_code == 200
    assert b"This domain is read-only during the account's capacity grace period." in detail.content
    assert response.status_code == 302
    assert response.url == reverse("domains")
    assert not conversation.tags.exists()
    assert Domain.objects.filter(id=extra.id, inbound_ready=True).exists()


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="sk_test_example",
    STRIPE_WEBHOOK_SECRET="whsec_example",
    STRIPE_PRO_UNIT_AMOUNT=499,
    STRIPE_PRO_CURRENCY="usd",
)
def test_checkout_reuses_saved_customer(monkeypatch, free_owner):
    BillingProfile.objects.create(user=free_owner, stripe_customer_id="cus_existing")
    create = Mock(return_value=SimpleNamespace(url="https://checkout.stripe.test/session"))
    monkeypatch.setattr(stripe.checkout.Session, "create", create)

    url = create_checkout_url(free_owner)

    assert url == "https://checkout.stripe.test/session"
    assert create.call_args.kwargs["customer"] == "cus_existing"
    assert create.call_args.kwargs["line_items"] == [
        {
            "price_data": {
                "currency": "usd",
                "unit_amount": 499,
                "recurring": {"interval": "month"},
                "product_data": {"name": "Operational Inbox Pro"},
            },
            "quantity": 1,
        }
    ]
    assert (
        create.call_args.kwargs["subscription_data"]["metadata"]["operational_inbox_plan"] == "pro"
    )


@pytest.mark.django_db
@override_settings(STRIPE_SECRET_KEY="sk_test_example")
def test_subscription_webhooks_are_idempotent_and_do_not_regress(monkeypatch, free_owner):
    profile = BillingProfile.objects.create(user=free_owner, stripe_customer_id="cus_test")
    subscription = {
        "id": "sub_test",
        "customer": "cus_test",
        "status": "active",
        "metadata": {"operational_inbox_plan": "pro"},
        "cancel_at_period_end": False,
        "items": {
            "data": [
                {
                    "price": {"id": "price_pro"},
                    "current_period_end": 2_000_000_000,
                }
            ]
        },
    }
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda value: subscription)
    active = stripe.Event.construct_from(
        {
            "id": "evt_active",
            "type": "customer.subscription.updated",
            "created": 200,
            "data": {"object": subscription},
        },
        "sk_test_example",
    )

    assert process_event(active)
    assert not process_event(active)
    profile.refresh_from_db()
    assert profile.subscription_status == BillingProfile.SubscriptionStatus.ACTIVE
    assert profile.stripe_price_id == "price_pro"

    stale_deleted = stripe.Event.construct_from(
        {
            "id": "evt_stale",
            "type": "customer.subscription.deleted",
            "created": 100,
            "data": {"object": {**subscription, "status": "canceled"}},
        },
        "sk_test_example",
    )
    assert process_event(stale_deleted)
    profile.refresh_from_db()
    assert profile.subscription_status == BillingProfile.SubscriptionStatus.ACTIVE
    assert StripeWebhookEvent.objects.count() == 2


@pytest.mark.django_db
@override_settings(STRIPE_SECRET_KEY="sk_test_example", DOMAIN_DOWNGRADE_GRACE_DAYS=30)
def test_subscription_downgrade_starts_domain_capacity_grace(free_owner):
    first = make_domain(free_owner, "downgrade-primary.example")
    make_domain(free_owner, "downgrade-extra.example")
    profile = BillingProfile.objects.create(
        user=free_owner,
        stripe_customer_id="cus_downgrade",
        stripe_subscription_id="sub_downgrade",
        subscription_status=BillingProfile.SubscriptionStatus.ACTIVE,
        subscription_plan="pro",
    )
    event = stripe.Event.construct_from(
        {
            "id": "evt_downgrade",
            "type": "customer.subscription.deleted",
            "created": 300,
            "data": {
                "object": {
                    "id": "sub_downgrade",
                    "customer": "cus_downgrade",
                    "status": "canceled",
                    "metadata": {"operational_inbox_plan": "pro"},
                    "cancel_at_period_end": False,
                    "items": {"data": []},
                }
            },
        },
        "sk_test_example",
    )
    before = timezone.now()

    assert process_event(event)

    profile.refresh_from_db()
    assert profile.subscription_status == BillingProfile.SubscriptionStatus.CANCELED
    assert profile.free_primary_domain_id == first.id
    assert before + timedelta(days=30) <= profile.domain_grace_ends_at
    assert profile.domain_grace_ends_at <= timezone.now() + timedelta(days=30)
