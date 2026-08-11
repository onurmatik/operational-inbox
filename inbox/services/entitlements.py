from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db.models import QuerySet

from inbox.models import BillingProfile, Domain, User

FREE_DOMAIN_LIMIT = 1
PRO_DOMAIN_LIMIT = 20


class PlanRequired(ValidationError):
    pass


@dataclass(frozen=True)
class Entitlements:
    plan: str
    is_pro: bool
    domain_limit: int
    ai: bool
    outbound: bool
    custom_settings: bool


FREE_ENTITLEMENTS = Entitlements(
    plan="free",
    is_pro=False,
    domain_limit=FREE_DOMAIN_LIMIT,
    ai=False,
    outbound=False,
    custom_settings=False,
)
PRO_ENTITLEMENTS = Entitlements(
    plan="pro",
    is_pro=True,
    domain_limit=PRO_DOMAIN_LIMIT,
    ai=True,
    outbound=True,
    custom_settings=True,
)


def for_user(user: User | AnonymousUser) -> Entitlements:
    if not user.is_authenticated:
        return FREE_ENTITLEMENTS
    try:
        profile = user.billing_profile
    except BillingProfile.DoesNotExist:
        return FREE_ENTITLEMENTS
    if not profile.is_pro:
        return FREE_ENTITLEMENTS
    return Entitlements(
        plan=PRO_ENTITLEMENTS.plan,
        is_pro=True,
        domain_limit=settings.MAX_DOMAINS_PER_USER,
        ai=True,
        outbound=True,
        custom_settings=True,
    )


def active_domains(user: User) -> QuerySet[Domain]:
    return Domain.objects.filter(owner=user).exclude(status=Domain.Status.DISABLED)


def free_primary_domain(user: User) -> Domain | None:
    return active_domains(user).order_by("created_at", "id").first()


def can_manage_domain(user: User, domain: Domain) -> bool:
    entitlements = for_user(user)
    if entitlements.is_pro:
        return True
    primary = free_primary_domain(user)
    return primary is not None and primary.id == domain.id


def require_pro(user: User, feature: str = "This feature") -> None:
    if not for_user(user).is_pro:
        raise PlanRequired(f"{feature} requires Operational Inbox Pro.")
