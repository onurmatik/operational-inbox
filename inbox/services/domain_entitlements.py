from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    BillingProfile,
    Domain,
    DomainTest,
    DurableJob,
    InboundRoutingTransition,
    User,
)


@dataclass(frozen=True)
class DomainCapacity:
    used: int
    limit: int
    primary_domain_id: uuid.UUID | None
    grace_ends_at: datetime | None

    @property
    def over_capacity(self) -> bool:
        return self.used > self.limit


def _active_domains(user: User):
    return Domain.objects.filter(owner=user).exclude(status=Domain.Status.DISABLED)


def _selected_primary(profile: BillingProfile, domains: list[Domain]) -> Domain | None:
    if not domains:
        return None
    selected_id = profile.free_primary_domain_id
    return next((domain for domain in domains if domain.id == selected_id), domains[0])


def domain_capacity(user: User) -> DomainCapacity:
    from inbox.services.entitlements import for_user

    entitlements = for_user(user)
    domains = list(_active_domains(user).order_by("created_at", "id"))
    profile = BillingProfile.objects.filter(user=user).first()
    primary = (
        _selected_primary(profile, domains) if profile is not None else next(iter(domains), None)
    )
    return DomainCapacity(
        used=len(domains),
        limit=entitlements.domain_limit,
        primary_domain_id=primary.id if primary is not None else None,
        grace_ends_at=profile.domain_grace_ends_at if profile is not None else None,
    )


@transaction.atomic
def select_free_primary_domain(*, user: User, domain: Domain) -> DomainCapacity:
    profile, _ = BillingProfile.objects.get_or_create(user=user)
    profile = BillingProfile.objects.select_for_update().get(id=profile.id)
    if profile.is_pro:
        raise ValueError("A primary Free Core domain is only needed on Free Core.")
    try:
        domain = (
            Domain.objects.select_for_update()
            .exclude(status=Domain.Status.DISABLED)
            .get(id=domain.id, owner=user)
        )
    except Domain.DoesNotExist as exc:
        raise ValueError("The selected domain is not active for this account.") from exc
    profile.free_primary_domain = domain
    profile.save(update_fields=("free_primary_domain", "updated_at"))
    return domain_capacity(user)


def _disable_for_capacity(domain: Domain, *, now) -> bool:
    with transaction.atomic():
        locked = Domain.objects.select_for_update().get(id=domain.id)
        if locked.status == Domain.Status.DISABLED:
            return False
        locked.status = Domain.Status.DISABLED
        locked.inbound_ready = False
        locked.outbound_ready = False
        locked.outbound_status = Domain.OutboundStatus.DISABLED
        locked.outbound_error_code = ""
        locked.outbound_error_message = ""
        locked.save(
            update_fields=(
                "status",
                "inbound_ready",
                "outbound_ready",
                "outbound_status",
                "outbound_error_code",
                "outbound_error_message",
                "updated_at",
            )
        )
        locked.inbound_routes.update(is_active=False)
        locked.routing_transitions.filter(
            status__in=(
                InboundRoutingTransition.Status.PREPARING,
                InboundRoutingTransition.Status.WAITING_DNS,
                InboundRoutingTransition.Status.WAITING_TEST,
                InboundRoutingTransition.Status.GRACE,
                InboundRoutingTransition.Status.FAILED,
            )
        ).update(
            status=InboundRoutingTransition.Status.CANCELLED,
            cancelled_at=now,
            updated_at=now,
        )
        locked.tests.filter(status=DomainTest.Status.PENDING).update(
            status=DomainTest.Status.EXPIRED,
            updated_at=now,
        )
        DurableJob.objects.get_or_create(
            idempotency_key=f"receipt-rule:capacity-disable:{locked.id}",
            defaults={
                "domain": locked,
                "kind": "reconcile_receipt_rule",
                "payload": {},
                "due_at": now,
            },
        )
        AuditEvent.objects.create(
            domain=locked,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.disabled_after_capacity_grace",
            object_type="Domain",
            object_id=locked.id,
            request_id=f"capacity:{locked.id}",
            metadata={"grace_ended_at": now.isoformat()},
        )
    return True


@transaction.atomic
def reconcile_domain_capacity(*, user: User, now=None) -> tuple[DomainCapacity, int]:
    now = now or timezone.now()
    profile, _ = BillingProfile.objects.get_or_create(user=user)
    profile = BillingProfile.objects.select_for_update().get(id=profile.id)
    domains = list(_active_domains(user).select_for_update().order_by("created_at", "id"))
    primary = _selected_primary(profile, domains)
    changed_fields: list[str] = []

    if primary is not None and profile.free_primary_domain_id != primary.id:
        profile.free_primary_domain = primary
        changed_fields.append("free_primary_domain")

    if profile.is_pro or len(domains) <= 1:
        if profile.domain_grace_ends_at is not None:
            profile.domain_grace_ends_at = None
            changed_fields.append("domain_grace_ends_at")
        if changed_fields:
            profile.save(update_fields=(*changed_fields, "updated_at"))
        return domain_capacity(user), 0

    if profile.domain_grace_ends_at is None:
        profile.domain_grace_ends_at = now + timedelta(
            days=getattr(settings, "DOMAIN_DOWNGRADE_GRACE_DAYS", 30)
        )
        changed_fields.append("domain_grace_ends_at")
    if changed_fields:
        profile.save(update_fields=(*changed_fields, "updated_at"))

    if profile.domain_grace_ends_at > now or primary is None:
        return domain_capacity(user), 0

    disabled = sum(
        int(_disable_for_capacity(domain, now=now)) for domain in domains if domain.id != primary.id
    )
    profile.domain_grace_ends_at = None
    profile.save(update_fields=("domain_grace_ends_at", "updated_at"))
    return domain_capacity(user), disabled


def reconcile_all_domain_capacities(*, now=None) -> int:
    now = now or timezone.now()
    active_pro = Q(
        billing_profile__subscription_plan="pro",
        billing_profile__subscription_status__in=(
            BillingProfile.SubscriptionStatus.ACTIVE,
            BillingProfile.SubscriptionStatus.TRIALING,
            BillingProfile.SubscriptionStatus.PAST_DUE,
        ),
    )
    users = User.objects.annotate(
        active_domain_count=Count(
            "domains",
            filter=~Q(domains__status=Domain.Status.DISABLED),
        )
    ).filter(
        Q(billing_profile__domain_grace_ends_at__isnull=False)
        | (Q(active_domain_count__gt=1) & ~active_pro)
    )
    return sum(reconcile_domain_capacity(user=user, now=now)[1] for user in users)
