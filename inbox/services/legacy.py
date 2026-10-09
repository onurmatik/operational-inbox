"""Compatibility reads for retired features, backed by append-only audit snapshots."""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from inbox.models import AuditEvent, Domain


def legacy_records(domain: Domain, model_name: str):
    return (
        AuditEvent.objects.filter(domain=domain, object_type=model_name)
        .filter(Q(metadata__partial=False) | Q(metadata__partial__isnull=True))
        .filter(
            Q(event_type="legacy.record_archived")
            | Q(event_type="classification.overridden", metadata__record__isnull=False)
        )
    )


def legacy_payload(event: AuditEvent) -> dict[str, Any]:
    return dict(event.metadata.get("record", {}))


def redact_legacy_content(*, domain: Domain, cutoff: Any) -> None:
    """Apply content retention to snapshots as well as current mail records."""
    events = AuditEvent.all_objects.filter(
        domain=domain,
        object_type__in=("Report", "ReportItem", "Classification", "Notification"),
        metadata__record__isnull=False,
    )
    fields = {
        "Report": ("title", "content"),
        "ReportItem": ("summary",),
        "Classification": ("topic", "summary", "recommended_action"),
        "Notification": ("title", "body"),
    }
    for event in events.iterator():
        record = legacy_payload(event)
        timestamp = event.metadata.get("content_created_at") or record.get("created_at")
        created_at = parse_datetime(timestamp) if isinstance(timestamp, str) else None
        if created_at is None:
            created_at = event.created_at
        if timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at)
        if created_at > cutoff:
            continue
        for field in fields[event.object_type]:
            record[field] = ""
        if event.object_type == "Report":
            record["title"] = "Expired report"
        elif event.object_type == "Notification":
            record["title"] = "Expired notification"
        # Retention is the explicit exception to append-only audit storage.
        AuditEvent.all_objects.filter(id=event.id).update(
            metadata={**event.metadata, "record": record}
        )
