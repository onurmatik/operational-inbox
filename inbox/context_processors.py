from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from inbox.models import Conversation, Domain, Notification
from inbox.services.entitlements import can_manage_domain, for_user


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    if not request.user.is_authenticated:
        return {}
    domains = request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname")
    domain_id = request.session.get("domain_id")
    selected = domains.filter(id=domain_id).first() if domain_id else None
    selected = selected or domains.first()
    entitlements = for_user(request.user)
    if selected is None:
        return {"nav_domains": domains, "plan_entitlements": entitlements}
    return {
        "nav_domains": domains,
        "current_domain": selected,
        "current_domain_writable": can_manage_domain(request.user, selected),
        "plan_entitlements": entitlements,
        "nav_unread_notifications": Notification.objects.filter(
            domain=selected,
            channel=Notification.Channel.IN_APP,
            read_at__isnull=True,
        ).count(),
        "nav_quarantine_count": Conversation.objects.filter(
            domain=selected, status=Conversation.Status.QUARANTINED
        ).count(),
    }
