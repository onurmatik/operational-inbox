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
def test_free_billing_page_renders_upgrade_state(client, free_owner):
    make_domain(free_owner, "billing-free.example")
    client.force_login(free_owner)

    response = client.get(reverse("billing"))

    assert response.status_code == 200
    assert b"Current plan" in response.content
    assert b"Free" in response.content


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
        reverse("conversation_status", args=[conversation.id]),
        {"status": Conversation.Status.RESOLVED},
    )

    conversation.refresh_from_db()
    assert response.status_code == 302
    assert response.url == reverse("billing")
    assert conversation.status == Conversation.Status.OPEN
    assert Domain.objects.filter(id=extra.id, inbound_ready=True).exists()


@pytest.mark.django_db
@override_settings(
    STRIPE_SECRET_KEY="sk_test_example",
    STRIPE_WEBHOOK_SECRET="whsec_example",
    STRIPE_PRO_UNIT_AMOUNT=2900,
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
                "unit_amount": 2900,
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
