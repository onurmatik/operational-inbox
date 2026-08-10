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
    OutboundMessage,
)
from inbox.services.entitlements import can_manage_domain, for_user


def _inbox_url(domain_id: Any, **filters: str) -> str:
    return f"{reverse('inbox')}?{urlencode({'domain': str(domain_id), **filters})}"


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
        addresses_by_domain.setdefault(row["domain_id"], []).append(
            {
                "address": address,
                "normalized_address": address.casefold(),
                "local_part": address.rsplit("@", 1)[0],
                "new_count": row["new_count"],
                "url": _inbox_url(row["domain_id"], recipient=address),
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
                "url": _inbox_url(domain.id),
                "starred_url": _inbox_url(domain.id, folder="starred"),
                "archive_url": _inbox_url(domain.id, folder="archive"),
                "trash_url": _inbox_url(domain.id, folder="trash"),
                "addresses": addresses_by_domain.get(domain.id, []),
            }
        )

    selected_inbox_url = _inbox_url(selected.id)
    nav_folder = request.GET.get("folder", "inbox")
    nav_recipient = request.GET.get("recipient", "").strip().casefold()
    return {
        "nav_domains": domains,
        "nav_inbox_tree": inbox_tree,
        "current_domain": selected,
        "current_domain_writable": can_manage_domain(request.user, selected),
        "plan_entitlements": entitlements,
        "nav_quarantine_count": Conversation.objects.filter(
            domain=selected,
            messages__is_quarantined=True,
            archived_at__isnull=True,
            trashed_at__isnull=True,
        )
        .distinct()
        .count(),
        "nav_outbound_problem_count": OutboundMessage.objects.filter(
            domain_id__in=domain_ids,
            status__in={
                OutboundMessage.Status.FAILED,
                OutboundMessage.Status.UNKNOWN,
                OutboundMessage.Status.BOUNCED,
                OutboundMessage.Status.COMPLAINED,
            },
        ).count(),
        "nav_folder": nav_folder,
        "nav_recipient": nav_recipient,
        "nav_current_inbox_url": selected_inbox_url,
    }
