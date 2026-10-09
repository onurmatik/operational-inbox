from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from inbox.models import Domain
from inbox.services.entitlements import can_manage_domain, for_user


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    if not request.user.is_authenticated:
        return {}
    domains = list(request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname"))
    explicit = request.GET.get("domain") or request.POST.get("domain")
    selected = next((domain for domain in domains if str(domain.id) == explicit), None)
    return {
        "nav_domains": domains,
        "current_domain": selected,
        "current_domain_writable": selected and can_manage_domain(request.user, selected),
        "plan_entitlements": for_user(request.user),
    }
