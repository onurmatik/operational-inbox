from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from inbox.models import Conversation, Notification, Project


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    if not request.user.is_authenticated:
        return {}
    organizations = request.user.organizations.filter(is_active=True).order_by("name")
    organization_id = request.session.get("organization_id")
    selected = organizations.filter(id=organization_id).first() if organization_id else None
    selected = selected or organizations.first()
    if selected is None:
        return {"nav_organizations": organizations}
    project_id = request.session.get("project_id")
    current_project = (
        Project.objects.filter(organization=selected, id=project_id, is_active=True).first()
        if project_id
        else None
    )
    return {
        "nav_organizations": organizations,
        "current_organization": selected,
        "current_project": current_project,
        "nav_unread_notifications": Notification.objects.filter(
            organization=selected,
            channel=Notification.Channel.IN_APP,
            read_at__isnull=True,
        ).count(),
        "nav_quarantine_count": Conversation.objects.filter(
            organization=selected, status=Conversation.Status.QUARANTINED
        ).count(),
    }
