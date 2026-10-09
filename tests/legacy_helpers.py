"""Build the historical snapshots that the retirement migration produces."""

import json
import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from inbox.models import AuditEvent


def archived_record(domain, model_name, *, created_at=None, content_created_at=None, **record):
    created_at = created_at or timezone.now()
    record = {"id": uuid.uuid4(), "created_at": created_at, **record}
    event = AuditEvent.objects.create(
        domain=domain,
        actor_type="SYSTEM",
        event_type="legacy.record_archived",
        object_type=model_name,
        object_id=record["id"],
        metadata={
            "record": json.loads(json.dumps(record, cls=DjangoJSONEncoder)),
            "content_created_at": (content_created_at or created_at).isoformat(),
        },
    )
    AuditEvent.all_objects.filter(pk=event.pk).update(created_at=created_at)
    event.refresh_from_db()
    return event
