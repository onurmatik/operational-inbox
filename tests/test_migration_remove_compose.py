import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_remove_compose_deletes_test_data_and_preserves_replies(request):
    before = [("inbox", "0021_retire_conversation_autopilot")]
    after = [("inbox", "0023_reply_only_schema")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("inbox")
    request.addfinalizer(lambda: MigrationExecutor(connection).migrate(latest))
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps

    def rows(name):
        return apps.get_model("inbox", name).objects

    now = timezone.now()
    owner = rows("User").create(email="remove-compose@example.net", password="unused")
    domain = rows("Domain").create(
        owner=owner,
        hostname="cleanup.example",
        claim_expires_at=now + timedelta(days=1),
    )
    removed = set()
    empty_conversations = set()

    def audit(obj):
        rows("AuditEvent").create(
            domain=domain,
            actor_type="SYSTEM",
            event_type="test.record",
            object_type=obj._meta.object_name,
            object_id=obj.id,
            metadata={"body": "Test content"},
        )

    def make_send(conversation, status, context=None):
        draft = rows("ReplyDraft").create(
            domain=domain,
            conversation=conversation,
            kind="REPLY" if context else "COMPOSE",
            context_message=context,
        )
        revision = rows("ReplyDraftRevision").create(
            domain=domain,
            draft=draft,
            number=1,
            subject="Test",
            body_text="Body",
            from_address="" if context else "me@cleanup.example",
            to_address="" if context else "customer@example.net",
            content_hash="a" * 64,
        )
        draft.current_revision = revision
        draft.save()
        approval = rows("DraftApproval").create(
            domain=domain,
            revision=revision,
            approved_by=owner,
            content_hash=revision.content_hash,
        )
        outbound = rows("OutboundMessage").create(
            domain=domain,
            conversation=conversation,
            revision=revision,
            status=status,
            idempotency_key=uuid.uuid4(),
            from_address="me@cleanup.example",
            to_address="customer@example.net",
            subject="Test",
            body_text="Body",
            content_hash=revision.content_hash,
            rfc_message_id=f"<{revision.id}@cleanup.example>",
            provider_message_id=str(uuid.uuid4()),
        )
        message = rows("Message").create(
            domain=domain,
            conversation=conversation,
            direction="OUTBOUND",
            provider_message_id=outbound.provider_message_id,
            from_address=outbound.from_address,
            text_body=outbound.body_text,
            received_at=now - timedelta(days=1),
        )
        recipient = rows("MessageRecipient").create(
            domain=domain,
            message=message,
            kind="TO",
            address=outbound.to_address,
        )
        reference = rows("MessageReference").create(
            domain=domain,
            message=message,
            kind="MESSAGE_ID",
            position=0,
            value_hash="b" * 64,
        )
        event = rows("DeliveryEvent").create(
            domain=domain,
            outbound_message=outbound,
            message=message,
            provider_event_id=str(uuid.uuid4()),
            occurred_at=now,
            event_type="Send",
        )
        notification = rows("Notification").create(
            domain=domain,
            conversation=conversation,
            kind="outbound_problem",
            channel="EMAIL",
            dedupe_key=f"outbound:{outbound.id}:{status}",
            title="Test",
            body="Test content",
        )
        job = rows("DurableJob").create(
            domain=domain,
            kind="send_outbound",
            idempotency_key=f"outbound:{outbound.id}",
            status="LEASED",
            payload={"outbound_id": str(outbound.id)},
            due_at=now,
        )
        notification_job = rows("DurableJob").create(
            domain=domain,
            kind="send_notification",
            idempotency_key=f"notification:{notification.id}",
            status="COMPLETE",
            payload={"notification_id": str(notification.id)},
            due_at=now,
        )
        objects = [
            draft,
            revision,
            approval,
            outbound,
            message,
            recipient,
            reference,
            event,
            notification,
            job,
            notification_job,
        ]
        if context is None:
            removed.update(obj.id for obj in objects)
        for obj in objects:
            audit(obj)
        return outbound

    for status in (
        "QUEUED",
        "SUBMITTING",
        "UNKNOWN",
        "FAILED",
        "ACCEPTED",
        "DELIVERED",
        "BOUNCED",
        "COMPLAINED",
    ):
        conversation = rows("Conversation").create(
            domain=domain,
            subject=status,
            first_message_at=now - timedelta(days=2),
            last_message_at=now,
        )
        tag = rows("ConversationTag").create(
            domain=domain,
            conversation=conversation,
            name="Test",
            normalized_name="test",
        )
        audit(conversation)
        audit(tag)
        empty_conversations.add(conversation.id)
        removed.update((conversation.id, tag.id))
        original = make_send(conversation, status)
        # Resends share the Compose revision and must be removed as well.
        if status == "UNKNOWN":
            resend = rows("OutboundMessage").get(id=original.id)
            resend.pk = uuid.uuid4()
            resend.parent_id = original.id
            resend.attempt_number = 2
            resend.idempotency_key = None
            resend.provider_message_id = ""
            resend.save()
            removed.add(resend.id)
            audit(resend)

    mixed = rows("Conversation").create(
        domain=domain,
        subject="Mixed",
        first_message_at=now - timedelta(days=10),
        last_message_at=now + timedelta(days=10),
        last_outbound_at=now + timedelta(days=10),
    )
    make_send(mixed, "DELIVERED")
    inbound = rows("Message").create(
        domain=domain,
        conversation=mixed,
        direction="INBOUND",
        provider_message_id="real-inbound",
        from_address="customer@example.net",
        text_body="Keep this",
        received_at=now,
    )
    reply = make_send(mixed, "QUEUED", context=inbound)
    saved_key = reply.idempotency_key
    normal = rows("Conversation").create(
        domain=domain,
        subject="Normal reply",
        first_message_at=now,
        last_message_at=now,
    )
    normal_inbound = rows("Message").create(
        domain=domain,
        conversation=normal,
        direction="INBOUND",
        provider_message_id="normal-inbound",
        from_address="customer@example.net",
        text_body="Also keep this",
        received_at=now,
    )
    normal_reply = make_send(normal, "DELIVERED", context=normal_inbound)
    orphan = rows("Conversation").create(
        domain=domain,
        subject="Empty test",
        first_message_at=now,
        last_message_at=now,
    )
    orphan_draft = rows("ReplyDraft").create(domain=domain, conversation=orphan, kind="COMPOSE")
    removed.update((orphan.id, orphan_draft.id))
    archived = rows("AuditEvent").create(
        domain=domain,
        object_id=uuid.uuid4(),
        object_type="ReportItem",
        event_type="legacy.record_archived",
        metadata={"record": {"conversation_id": str(orphan.id), "summary": "Old test"}},
    )
    audit(mixed)
    audit(inbound)
    rows("AuditEvent").create(
        domain=domain,
        event_type="domain.test",
        object_type="Domain",
        object_id=domain.id,
    )
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    apps = executor.loader.project_state(after).apps

    for name in (
        "Conversation",
        "ReplyDraft",
        "ReplyDraftRevision",
        "DraftApproval",
        "OutboundMessage",
        "Message",
        "MessageRecipient",
        "MessageReference",
        "DeliveryEvent",
        "Notification",
        "DurableJob",
        "ConversationTag",
    ):
        assert not rows(name).filter(id__in=removed).exists(), name
    assert not rows("AuditEvent").filter(object_id__in=removed).exists()
    assert rows("AuditEvent").filter(object_id=domain.id).exists()
    assert rows("AuditEvent").filter(object_id=inbound.id).exists()
    assert rows("Message").get(id=inbound.id).text_body == "Keep this"
    assert rows("OutboundMessage").count() == 2
    assert rows("OutboundMessage").get(id=normal_reply.id).status == "DELIVERED"
    assert rows("Message").get(id=normal_inbound.id).text_body == "Also keep this"
    assert not rows("AuditEvent").filter(id=archived.id).exists()
    assert rows("OutboundMessage").get(id=reply.id).idempotency_key == saved_key
    assert rows("DurableJob").filter(payload__outbound_id=str(reply.id), status="LEASED").exists()
    mixed = rows("Conversation").get(id=mixed.id)
    assert mixed.last_message_at >= now and mixed.last_message_at < now + timedelta(minutes=1)
    assert mixed.last_outbound_at == now - timedelta(days=1)
    assert mixed.last_inbound_at == now
    draft_model = apps.get_model("inbox", "ReplyDraft")
    assert not draft_model._meta.get_field("context_message").null
    assert "kind" not in {field.name for field in draft_model._meta.fields}
    assert {"from_address", "to_address"}.isdisjoint(
        field.name for field in apps.get_model("inbox", "ReplyDraftRevision")._meta.fields
    )

    from inbox.services.ingestion import IngressEnvelope, process_delivery_notification

    assert (
        process_delivery_notification(
            IngressEnvelope(
                sns_message_id="late-deleted-compose",
                topic_arn="test",
                payload_digest="",
                notification={
                    "eventType": "Delivery",
                    "mail": {
                        "messageId": original.provider_message_id,
                        "tags": {"outbound_id": [str(original.id)]},
                    },
                },
            )
        )
        is False
    )
    assert rows("OutboundMessage").count() == 2
    assert not rows("DeliveryEvent").filter(provider_event_id="late-deleted-compose").exists()
