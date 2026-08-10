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

    with pytest.raises(ValidationError, match="at most 1"):
        create_domain(
            owner=free_owner,
            hostname="second.example",
            setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        )


@pytest.mark.django_db
def test_free_api_returns_upgrade_required(client, free_owner):
    domain = make_domain(free_owner, "api-free.example")
    client.force_login(free_owner)

    response = client.get(f"/api/v1/domains/{domain.id}")

    assert response.status_code == 403
    assert response.json()["code"] == "upgrade_required"


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
    ("domain_status", "inbound_ready", "outbound_status"),
    [
        (Domain.Status.PENDING_TEST, False, Domain.OutboundStatus.DISABLED),
        (Domain.Status.READY, True, Domain.OutboundStatus.ERROR),
    ],
)
def test_free_domain_detail_links_outbound_setup_to_upgrade(
    client, free_owner, domain_status, inbound_ready, outbound_status
):
    domain = make_domain(free_owner, "outbound-upgrade.example")
    domain.status = domain_status
    domain.inbound_ready = inbound_ready
    domain.outbound_status = outbound_status
    domain.save(update_fields=("status", "inbound_ready", "outbound_status", "updated_at"))
    client.force_login(free_owner)

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Inbound receiving remains available on Free" not in response.content
    assert b"Outbound sending requires" not in response.content
    assert b"Upgrade to set up outbound sending for this domain." in response.content
    assert (
        f'<a href="{reverse("billing")}" class="oi-button-secondary mt-3">Set up outbound</a>'
    ).encode() in response.content
    assert b"Enable sending" not in response.content
    assert b"Retry sending setup" not in response.content
    assert reverse("domain_enable_outbound", args=[domain.id]).encode() not in response.content


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
    assert b"Receive at any address" in response.content
    assert b"no per-address fee" in response.content
    assert b"All-domain agent feed, API &amp; agent-authored replies" in response.content
    assert f'method="post" action="{reverse("billing_checkout")}"'.encode() in response.content
    assert "Upgrade to Pro · USD 4.99/month".encode() in response.content
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
    assert b"Pro price" in response.content
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

    response = client.post(
        reverse("conversation_tag", args=[conversation.id]),
        {"operation": "add", "tag": "agent-reviewed"},
    )

    conversation.refresh_from_db()
    assert response.status_code == 302
    assert response.url == reverse("billing")
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
