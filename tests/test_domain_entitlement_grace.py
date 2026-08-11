from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from inbox.models import AuditEvent, BillingProfile, Domain, DurableJob, InboundRoute, User
from inbox.services.domain_entitlements import (
    domain_capacity,
    reconcile_all_domain_capacities,
    reconcile_domain_capacity,
    select_free_primary_domain,
)


def make_domain(owner: User, hostname: str) -> Domain:
    domain = Domain.objects.create(
        owner=owner,
        hostname=hostname,
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        ownership_verified=True,
        inbound_ready=True,
        outbound_ready=True,
        outbound_status=Domain.OutboundStatus.READY,
        claim_expires_at=timezone.now() + timedelta(days=3),
    )
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.DIRECT_DOMAIN,
        local_part="",
        address=hostname,
        is_active=True,
    )
    return domain


@pytest.fixture
def free_owner(db) -> User:
    return User.objects.create_user(
        email="capacity-free@example.com",
        password="Correct-Horse-Battery-901",
        email_verified_at=timezone.now(),
        is_active=True,
    )


@pytest.mark.django_db
@override_settings(DOMAIN_DOWNGRADE_GRACE_DAYS=30)
def test_free_over_capacity_starts_grace_and_selects_oldest(free_owner):
    oldest = make_domain(free_owner, "oldest.example")
    make_domain(free_owner, "newer.example")
    now = timezone.now()

    capacity, disabled = reconcile_domain_capacity(user=free_owner, now=now)

    assert disabled == 0
    assert capacity.used == 2
    assert capacity.limit == 1
    assert capacity.primary_domain_id == oldest.id
    assert capacity.grace_ends_at == now + timedelta(days=30)


@pytest.mark.django_db
def test_selected_domain_remains_after_capacity_grace(free_owner):
    old_primary = make_domain(free_owner, "old-primary.example")
    selected = make_domain(free_owner, "selected.example")
    now = timezone.now()
    reconcile_domain_capacity(user=free_owner, now=now)
    select_free_primary_domain(user=free_owner, domain=selected)
    profile = BillingProfile.objects.get(user=free_owner)
    profile.domain_grace_ends_at = now - timedelta(seconds=1)
    profile.save(update_fields=("domain_grace_ends_at", "updated_at"))

    capacity, disabled = reconcile_domain_capacity(user=free_owner, now=now)

    old_primary.refresh_from_db()
    selected.refresh_from_db()
    assert disabled == 1
    assert old_primary.status == Domain.Status.DISABLED
    assert not old_primary.inbound_routes.filter(is_active=True).exists()
    assert selected.status == Domain.Status.READY
    assert capacity.used == capacity.limit == 1
    assert capacity.primary_domain_id == selected.id
    assert capacity.grace_ends_at is None
    assert DurableJob.objects.filter(
        idempotency_key=f"receipt-rule:capacity-disable:{old_primary.id}"
    ).exists()
    assert AuditEvent.objects.filter(
        domain=old_primary,
        event_type="domain.disabled_after_capacity_grace",
    ).exists()


@pytest.mark.django_db
def test_pro_account_does_not_enter_domain_capacity_grace(owner):
    make_domain(owner, "first-pro.example")
    make_domain(owner, "second-pro.example")

    capacity, disabled = reconcile_domain_capacity(user=owner)

    assert disabled == 0
    assert capacity.used == 2
    assert capacity.limit == 20
    assert capacity.grace_ends_at is None
    assert domain_capacity(owner) == capacity


@pytest.mark.django_db
def test_capacity_sweep_starts_grace_for_free_owner_without_profile(free_owner):
    make_domain(free_owner, "sweep-first.example")
    make_domain(free_owner, "sweep-second.example")
    now = timezone.now()

    assert reconcile_all_domain_capacities(now=now) == 0

    profile = BillingProfile.objects.get(user=free_owner)
    assert profile.domain_grace_ends_at == now + timedelta(days=30)
