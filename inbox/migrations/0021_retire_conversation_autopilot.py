"""Retire internal handling without discarding mail, drafts or delivery evidence.

Stop old workers using the deployment contract before applying this migration.
"""
import json
import uuid
from django.core.serializers.json import DjangoJSONEncoder
from django.db import migrations, models
from django.utils import timezone

ARCHIVE_REQUEST = "migration:0021:retire-autopilot"


def retire_handling(apps, schema_editor):
    alias = schema_editor.connection.alias
    Audit = apps.get_model("inbox", "AuditEvent")
    Outbound = apps.get_model("inbox", "OutboundMessage")
    Draft = apps.get_model("inbox", "ReplyDraft")
    Revision = apps.get_model("inbox", "ReplyDraftRevision")
    Job = apps.get_model("inbox", "DurableJob")
    now = timezone.now()

    def archive(row, fields=None):
        values = {f.attname: getattr(row, f.attname) for f in row._meta.concrete_fields
                  if fields is None or f.attname in fields}
        record = json.loads(json.dumps(values, cls=DjangoJSONEncoder))
        content_at = (row.context_message.received_at if row._meta.object_name in
                      {"ConversationHandling", "HandlingRun"} else row.created_at)
        event = Audit.objects.using(alias).create(
            domain_id=row.domain_id, actor_type="SYSTEM",
            event_type="legacy.record_archived", object_type=row._meta.object_name,
            object_id=row.id, request_id=ARCHIVE_REQUEST,
            metadata={"record": record, "partial": fields is not None,
                      "content_created_at": content_at.isoformat()},
        )
        Audit.objects.using(alias).filter(id=event.id).update(created_at=row.created_at)

    for name in ("ConversationHandling", "HandlingRun", "HandlingPolicy"):
        for row in apps.get_model("inbox", name).objects.using(alias).all().iterator():
            # Account settings are retired configuration; only domain-scoped history is archived.
            if row.domain_id:
                archive(row)
    for outbound in Outbound.objects.using(alias).exclude(agent_run_id=None).iterator():
        archive(outbound, {"agent_run_id"})

    for outbound in Outbound.objects.using(alias).filter(
        authorization_mode="AGENT_POLICY", status="QUEUED"
    ).iterator():
        Outbound.objects.using(alias).filter(id=outbound.id).update(
            status="FAILED", failed_at=now, updated_at=now,
            error_code="autopilot_removed",
            error_message="Automatic reply cancelled. Review the saved draft to send it yourself.",
        )
        Job.objects.using(alias).filter(
            kind="send_outbound", payload__outbound_id=str(outbound.id),
            status__in=("PENDING", "LEASED", "RETRY"),
        ).update(status="COMPLETE", leased_until=None, last_error_code="feature_retired", updated_at=now)
        draft = Draft.objects.using(alias).get(id=outbound.revision.draft_id)
        if draft.current_revision_id == outbound.revision_id:
            original = outbound.revision
            revision = Revision.objects.using(alias).create(
                id=uuid.uuid4(), domain_id=original.domain_id, draft_id=draft.id,
                number=original.number + 1, subject=original.subject, body_text=original.body_text,
                from_address=original.from_address, to_address=original.to_address,
                content_hash=original.content_hash, author_id=original.author_id,
                is_agent_generated=original.is_agent_generated,
            )
            Draft.objects.using(alias).filter(id=draft.id).update(
                current_revision_id=revision.id, updated_at=now,
            )
    Job.objects.using(alias).filter(
        kind="handle_conversation", status__in=("PENDING", "LEASED", "RETRY"),
    ).update(status="COMPLETE", leased_until=None, last_error_code="feature_retired", updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("inbox", "0020_conversation_reply_briefs")]
    operations = [
        migrations.RunPython(retire_handling, migrations.RunPython.noop),
        migrations.RenameModel(old_name="HandlingUsage", new_name="ReplyAssistUsage"),
        migrations.RemoveConstraint(model_name="replyassistusage", name="uniq_handling_usage_day"),
        migrations.AddConstraint(model_name="replyassistusage", constraint=models.UniqueConstraint(
            fields=("owner", "day"), name="uniq_reply_assist_usage_day")),
        migrations.RemoveConstraint(model_name="outboundmessage", name="uniq_handling_run_send"),
        migrations.RemoveField(model_name="outboundmessage", name="agent_run"),
        migrations.DeleteModel(name="ConversationHandling"),
        migrations.DeleteModel(name="HandlingRun"),
        migrations.DeleteModel(name="HandlingPolicy"),
    ]
