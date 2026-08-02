from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import stripe
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction

from inbox.models import BillingProfile, StripeWebhookEvent, User


def billing_configured() -> bool:
    return bool(
        settings.STRIPE_SECRET_KEY
        and settings.STRIPE_WEBHOOK_SECRET
        and settings.STRIPE_PRO_UNIT_AMOUNT > 0
        and len(settings.STRIPE_PRO_CURRENCY) == 3
        and settings.STRIPE_PRO_CURRENCY.isalpha()
    )


def _configure_stripe() -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured("Stripe billing is not configured.")
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _profile(user: User) -> BillingProfile:
    profile, _ = BillingProfile.objects.get_or_create(user=user)
    return profile


def ensure_customer(user: User) -> BillingProfile:
    _configure_stripe()
    profile = _profile(user)
    if profile.stripe_customer_id:
        return profile
    customer = stripe.Customer.create(
        email=user.email,
        metadata={"operational_inbox_user_id": str(user.id)},
        idempotency_key=f"operational-inbox-customer-{user.id}",
    )
    profile.stripe_customer_id = customer.id
    profile.save(update_fields=("stripe_customer_id", "updated_at"))
    return profile


def create_checkout_url(user: User) -> str:
    if not billing_configured():
        raise ImproperlyConfigured("Stripe Pro billing is not configured.")
    profile = ensure_customer(user)
    if profile.is_pro:
        raise ValidationError("This account already has Pro access.")
    if not profile.stripe_customer_id:
        raise RuntimeError("Stripe customer creation did not return an identifier.")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=profile.stripe_customer_id,
        client_reference_id=str(user.id),
        line_items=[
            {
                "price_data": {
                    "currency": settings.STRIPE_PRO_CURRENCY,
                    "unit_amount": settings.STRIPE_PRO_UNIT_AMOUNT,
                    "recurring": {"interval": "month"},
                    "product_data": {"name": "Operational Inbox Pro"},
                },
                "quantity": 1,
            }
        ],
        subscription_data={
            "metadata": {
                "operational_inbox_user_id": str(user.id),
                "operational_inbox_plan": "pro",
            }
        },
        success_url=f"{settings.PUBLIC_BASE_URL}/app/billing/?checkout=success",
        cancel_url=f"{settings.PUBLIC_BASE_URL}/app/billing/?checkout=cancelled",
    )
    if not session.url:
        raise RuntimeError("Stripe Checkout did not return a redirect URL.")
    return session.url


def create_portal_url(user: User) -> str:
    profile = ensure_customer(user)
    if not profile.stripe_customer_id:
        raise RuntimeError("Stripe customer creation did not return an identifier.")
    session = stripe.billing_portal.Session.create(
        customer=profile.stripe_customer_id,
        return_url=f"{settings.PUBLIC_BASE_URL}/app/billing/",
    )
    return session.url


def price_summary() -> dict[str, Any] | None:
    if not billing_configured():
        return None
    unit_amount = settings.STRIPE_PRO_UNIT_AMOUNT
    compare_at_unit_amount = settings.STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT
    is_promotional = (
        settings.STRIPE_PRO_CURRENCY == "usd"
        and unit_amount == 499
        and compare_at_unit_amount == 999
    )
    return {
        "unit_amount": unit_amount,
        "amount": f"{unit_amount / 100:,.2f}",
        "compare_at_unit_amount": compare_at_unit_amount if is_promotional else None,
        "compare_at_amount": (
            f"{compare_at_unit_amount / 100:,.2f}" if is_promotional else None
        ),
        "currency": settings.STRIPE_PRO_CURRENCY.upper(),
        "interval": "month",
        "is_promotional": is_promotional,
        "product_name": "Operational Inbox Pro",
    }


def construct_event(payload: bytes, signature: str) -> stripe.Event:
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise ImproperlyConfigured("Stripe webhook signing secret is not configured.")
    return stripe.Webhook.construct_event(
        payload=payload,
        sig_header=signature,
        secret=settings.STRIPE_WEBHOOK_SECRET,
    )


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


def _subscription_period_end(subscription: Any) -> datetime | None:
    direct = subscription.get("current_period_end")
    if direct:
        return _timestamp(direct)
    item_periods = [
        item.get("current_period_end")
        for item in (subscription.get("items") or {}).get("data", [])
        if item.get("current_period_end")
    ]
    return _timestamp(max(item_periods)) if item_periods else None


def _subscription_price_id(subscription: Any) -> str:
    items = (subscription.get("items") or {}).get("data", [])
    if not items:
        return ""
    return str((items[0].get("price") or {}).get("id", ""))


def _profile_for_object(obj: Any) -> BillingProfile | None:
    customer_id = obj.get("customer")
    if customer_id:
        profile = BillingProfile.objects.filter(stripe_customer_id=str(customer_id)).first()
        if profile:
            return profile
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("operational_inbox_user_id") or obj.get("client_reference_id")
    if not user_id:
        return None
    try:
        user = User.objects.get(id=user_id)
    except (User.DoesNotExist, ValueError):
        return None
    profile = _profile(user)
    if customer_id and not profile.stripe_customer_id:
        profile.stripe_customer_id = str(customer_id)
        profile.save(update_fields=("stripe_customer_id", "updated_at"))
    return profile


def _apply_subscription(profile: BillingProfile, subscription: Any, *, event_created: int) -> None:
    if event_created < profile.last_stripe_event_created:
        return
    status = str(subscription.get("status", BillingProfile.SubscriptionStatus.NONE))
    allowed = set(BillingProfile.SubscriptionStatus.values)
    profile.stripe_subscription_id = str(subscription.get("id"))
    profile.stripe_price_id = _subscription_price_id(subscription)
    profile.subscription_plan = str(
        (subscription.get("metadata") or {}).get("operational_inbox_plan", "")
    )
    profile.subscription_status = (
        status if status in allowed else BillingProfile.SubscriptionStatus.NONE
    )
    profile.current_period_end = _subscription_period_end(subscription)
    profile.cancel_at_period_end = bool(subscription.get("cancel_at_period_end", False))
    profile.last_stripe_event_created = event_created
    profile.save(
        update_fields=(
            "stripe_subscription_id",
            "stripe_price_id",
            "subscription_plan",
            "subscription_status",
            "current_period_end",
            "cancel_at_period_end",
            "last_stripe_event_created",
            "updated_at",
        )
    )


def process_event(event: stripe.Event) -> bool:
    event_id = str(event.id)
    event_type = str(event.type)
    event_created = int(event.created)
    obj = event.data.object
    with transaction.atomic():
        _, created = StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event_id,
            defaults={"event_type": event_type, "stripe_created": event_created},
        )
        if not created:
            return False
        profile = _profile_for_object(obj)
        if profile is None:
            return True
        if event_type == "checkout.session.completed":
            subscription_id = obj.get("subscription")
            if subscription_id:
                _configure_stripe()
                subscription = stripe.Subscription.retrieve(str(subscription_id))
                _apply_subscription(profile, subscription, event_created=event_created)
        elif event_type.startswith("customer.subscription."):
            if event_type != "customer.subscription.deleted":
                _configure_stripe()
                with suppress(stripe.InvalidRequestError):
                    obj = stripe.Subscription.retrieve(str(obj.get("id")))
            _apply_subscription(profile, obj, event_created=event_created)
        return True
