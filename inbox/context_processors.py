from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.http import HttpRequest
from django.urls import reverse

from inbox.models import (
    Conversation,
    Domain,
    Message,
    MessageRecipient,
    Notification,
)
from inbox.services.entitlements import can_manage_domain, for_user


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    if not request.user.is_authenticated:
        return {}
    domains = list(request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname"))
    domain_id = request.session.get("domain_id")
    selected = next((domain for domain in domains if str(domain.id) == domain_id), None)
    selected = selected or (domains[0] if domains else None)
    entitlements = for_user(request.user)
    if selected is None:
        return {"nav_domains": domains, "plan_entitlements": entitlements}

    domain_ids = [domain.id for domain in domains]
    domain_new_counts = {
        row["domain_id"]: row["new_count"]
        for row in (
            Message.objects.filter(
                domain_id__in=domain_ids,
                direction=Message.Direction.INBOUND,
                viewed_at__isnull=True,
                conversation__archived_at__isnull=True,
                conversation__trashed_at__isnull=True,
            )
            .values("domain_id")
            .annotate(new_count=Count("id", distinct=True))
        )
    }
    address_rows = (
        MessageRecipient.objects.filter(
            domain_id__in=domain_ids,
            message__direction=Message.Direction.INBOUND,
            is_routing_recipient=True,
        )
        .values("domain_id", "address")
        .annotate(
            new_count=Count(
                "message_id",
                filter=Q(
                    message__viewed_at__isnull=True,
                    message__conversation__archived_at__isnull=True,
                    message__conversation__trashed_at__isnull=True,
                ),
                distinct=True,
            )
        )
        .order_by("domain_id", "-new_count", "address")
    )
    addresses_by_domain: dict[Any, list[dict[str, Any]]] = {}
    for row in address_rows:
        address = row["address"]
        params = {"domain": str(row["domain_id"]), "recipient": address}
        addresses_by_domain.setdefault(row["domain_id"], []).append(
            {
                "address": address,
                "normalized_address": address.casefold(),
                "local_part": address.rsplit("@", 1)[0],
                "new_count": row["new_count"],
                "url": f"{reverse('inbox')}?{urlencode(params)}",
            }
        )

    inbox_tree = []
    for domain in domains:
        new_count = domain_new_counts.get(domain.id, 0)
        inbox_tree.append(
            {
                "id": domain.id,
                "hostname": domain.hostname,
                "new_count": new_count,
                "open": domain.id == selected.id or new_count > 0,
                "url": f"{reverse('inbox')}?{urlencode({'domain': str(domain.id)})}",
                "addresses": addresses_by_domain.get(domain.id, []),
            }
        )

    selected_params = {"domain": str(selected.id)}
    selected_inbox_url = f"{reverse('inbox')}?{urlencode(selected_params)}"
    nav_folder = request.GET.get("folder", "inbox")
    nav_recipient = request.GET.get("recipient", "").strip().casefold()
    return {
        "nav_domains": domains,
        "nav_inbox_tree": inbox_tree,
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
        "nav_folder": nav_folder,
        "nav_recipient": nav_recipient,
        "nav_current_inbox_url": selected_inbox_url,
        "nav_starred_url": f"{selected_inbox_url}&folder=starred",
        "nav_archive_url": f"{selected_inbox_url}&folder=archive",
        "nav_trash_url": f"{selected_inbox_url}&folder=trash",
    }
