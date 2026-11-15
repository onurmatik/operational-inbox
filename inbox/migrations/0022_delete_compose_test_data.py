"""Delete Compose test data before the separate reply-only schema migration.

Apply with web, MCP and workers stopped through the deployment contract. Deleted
test data is not archived and cannot be restored by reversing this migration.
"""

from django.db import migrations
from django.db.models import Q


def remove_compose(apps, schema_editor):
    alias = schema_editor.connection.alias

    def rows(name):
        return apps.get_model("inbox", name).objects.using(alias)

    drafts = rows("ReplyDraft").filter(kind="COMPOSE")
    draft_ids = list(drafts.values_list("id", flat=True))
    if not draft_ids:
        return
    conversation_ids = set(drafts.values_list("conversation_id", flat=True))
    revisions = rows("ReplyDraftRevision").filter(draft_id__in=draft_ids)
    revision_ids = list(revisions.values_list("id", flat=True))
    outbound = rows("OutboundMessage").filter(revision_id__in=revision_ids)
    outbound_ids = list(outbound.values_list("id", flat=True))
    message_ids = set()
    for domain_id, provider_id in outbound.exclude(provider_message_id="").values_list(
        "domain_id", "provider_message_id"
    ):
        message_ids.update(
            rows("Message")
            .filter(
                domain_id=domain_id,
                provider_message_id=provider_id,
                direction="OUTBOUND",
            )
            .values_list("id", flat=True)
        )

    # Notifications and jobs reference sends by string IDs rather than foreign keys.
    notification_ids = set()
    for outbound_id in outbound_ids:
        notification_ids.update(
            rows("Notification")
            .filter(
                kind="outbound_problem",
                dedupe_key__startswith=f"outbound:{outbound_id}:",
            )
            .values_list("id", flat=True)
        )
    empty_conversation_ids = set()
    for conversation_id in conversation_ids:
        if (
            not rows("Message")
            .filter(conversation_id=conversation_id)
            .exclude(id__in=message_ids)
            .exists()
            and not rows("ReplyDraft")
            .filter(conversation_id=conversation_id)
            .exclude(id__in=draft_ids)
            .exists()
            and not rows("OutboundMessage")
            .filter(conversation_id=conversation_id)
            .exclude(id__in=outbound_ids)
            .exists()
        ):
            empty_conversation_ids.add(conversation_id)
    notification_ids.update(
        rows("Notification")
        .filter(
            conversation_id__in=empty_conversation_ids,
        )
        .values_list("id", flat=True)
    )
    jobs = rows("DurableJob").filter(
        Q(kind="send_outbound", payload__outbound_id__in=[str(pk) for pk in outbound_ids])
        | Q(kind="send_outbound", idempotency_key__in=[f"outbound:{pk}" for pk in outbound_ids])
        | Q(
            kind="send_notification",
            payload__notification_id__in=[str(pk) for pk in notification_ids],
        )
    )
    events = rows("DeliveryEvent").filter(
        Q(outbound_message_id__in=outbound_ids) | Q(message_id__in=message_ids)
    )
    approvals = rows("DraftApproval").filter(revision_id__in=revision_ids)
    related = [
        rows("MessageRecipient").filter(message_id__in=message_ids),
        rows("MessageReference").filter(message_id__in=message_ids),
        rows("Attachment").filter(message_id__in=message_ids),
        rows("ConversationTag").filter(conversation_id__in=empty_conversation_ids),
        approvals,
        events,
        jobs,
    ]
    removed_ids = set(draft_ids) | set(revision_ids) | set(outbound_ids)
    removed_ids |= message_ids | notification_ids | empty_conversation_ids
    for queryset in related:
        removed_ids.update(queryset.values_list("id", flat=True))
    audit_filter = Q(object_id__in=removed_ids)
    for key, ids in (
        ("outbound_id", outbound_ids),
        ("revision_id", revision_ids),
        ("draft_id", draft_ids),
        ("message_id", message_ids),
        ("notification_id", notification_ids),
        ("conversation_id", empty_conversation_ids),
    ):
        audit_filter |= Q(**{f"metadata__{key}__in": [str(pk) for pk in ids]})
        audit_filter |= Q(**{f"metadata__record__{key}__in": [str(pk) for pk in ids]})
    # Historical models intentionally bypass the live append-only audit manager.
    rows("AuditEvent").filter(audit_filter).delete()
    jobs.delete()
    rows("Notification").filter(id__in=notification_ids).delete()
    events.delete()
    outbound.delete()  # Outbound protects its revision; delete attempts first.
    drafts.delete()  # Cascades revisions and approvals.
    rows("Message").filter(id__in=message_ids).delete()
    rows("Conversation").filter(id__in=empty_conversation_ids).delete()

    for conversation in rows("Conversation").filter(
        id__in=conversation_ids - empty_conversation_ids
    ):
        remaining = list(
            rows("Message")
            .filter(conversation_id=conversation.id)
            .values_list(
                "received_at",
                "direction",
            )
        )
        inbound_at = [at for at, direction in remaining if direction == "INBOUND"]
        outbound_at = [at for at, direction in remaining if direction == "OUTBOUND"]
        activity = [at for at, _ in remaining]
        for created, accepted in (
            rows("OutboundMessage")
            .filter(conversation_id=conversation.id)
            .values_list("created_at", "accepted_at")
        ):
            activity.append(accepted or created)
            if accepted:
                outbound_at.append(accepted)
        if activity:
            rows("Conversation").filter(id=conversation.id).update(
                first_message_at=min(activity),
                last_message_at=max(activity),
                last_inbound_at=max(inbound_at, default=None),
                last_outbound_at=max(outbound_at, default=None),
            )


class Migration(migrations.Migration):
    dependencies = [("inbox", "0021_retire_conversation_autopilot")]
    operations = [
        migrations.RunPython(remove_compose, migrations.RunPython.noop),
    ]
