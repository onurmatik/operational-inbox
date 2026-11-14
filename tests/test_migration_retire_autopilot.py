from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_migration_retires_work_preserves_drafts_history_and_usage(request):
    before = [("inbox", "0020_conversation_reply_briefs")]
    after = [("inbox", "0021_retire_conversation_autopilot")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("inbox")
    request.addfinalizer(lambda: MigrationExecutor(connection).migrate(latest))
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps

    def get(name):
        return apps.get_model("inbox", name)

    now = timezone.now()
    owner = get("User").objects.create(email="retire@example.com", password="unused")
    domain = get("Domain").objects.create(
        owner=owner,
        hostname="retire.example",
        setup_mode="DIRECT_MX",
        claim_expires_at=now + timedelta(days=1),
    )
    get("HandlingUsage").objects.create(
        owner=owner, day=now.date(), requests=17, input_tokens=100, output_tokens=50
    )
    get("HandlingPolicy").objects.create(owner=owner, domain=domain, knowledge="Old knowledge")
    ids = {}
    for i, status in enumerate(("QUEUED", "DELIVERED", "SUBMITTING", "UNKNOWN")):
        conversation = get("Conversation").objects.create(
            domain=domain, subject=status, first_message_at=now, last_message_at=now
        )
        message = get("Message").objects.create(
            domain=domain,
            conversation=conversation,
            direction="INBOUND",
            provider_message_id=status,
            rfc_message_id=f"<{status}@example.net>",
            from_address="sender@example.net",
            received_at=now,
            text_body="Original mail",
        )
        draft = get("ReplyDraft").objects.create(
            domain=domain, conversation=conversation, context_message=message
        )
        revision = get("ReplyDraftRevision").objects.create(
            domain=domain,
            draft=draft,
            number=1,
            subject="Reply",
            body_text=f"Original {status} reply",
            content_hash="a" * 64,
            is_agent_generated=True,
        )
        draft.current_revision = revision
        draft.save()
        run = get("HandlingRun").objects.create(
            domain=domain,
            conversation=conversation,
            context_message=message,
            generation=1,
            draft=draft,
            reply_brief={"instructions": "Private instruction", "delivery": "auto"},
            reason="Private reason",
        )
        get("ConversationHandling").objects.create(
            domain=domain,
            conversation=conversation,
            context_message=message,
            followup_at=now,
            reply_brief=run.reply_brief,
        )
        outbound = get("OutboundMessage").objects.create(
            domain=domain,
            conversation=conversation,
            revision=revision,
            agent_run=run,
            authorization_mode="AGENT_POLICY",
            status=status,
            from_address="me@retire.example",
            to_address="sender@example.net",
            subject=revision.subject,
            body_text=revision.body_text,
            content_hash=revision.content_hash,
            rfc_message_id=f"<out-{i}@retire.example>",
        )
        get("DurableJob").objects.create(
            domain=domain,
            kind="send_outbound",
            idempotency_key=f"out-{i}",
            status="LEASED",
            due_at=now,
            payload={"outbound_id": str(outbound.id)},
        )
        get("DurableJob").objects.create(
            domain=domain,
            kind="handle_conversation",
            idempotency_key=f"run-{i}",
            status="RETRY",
            due_at=now,
            payload={"run_id": str(run.id)},
        )
        ids[status] = (outbound.id, draft.id, revision.id)
    executor = MigrationExecutor(connection)
    executor.migrate(after)
    apps = executor.loader.project_state(after).apps
    for status, (out_id, draft_id, rev_id) in ids.items():
        outbound = get("OutboundMessage").objects.get(id=out_id)
        draft = get("ReplyDraft").objects.get(id=draft_id)
        assert (
            get("ReplyDraftRevision").objects.get(id=rev_id).body_text == f"Original {status} reply"
        )
        assert outbound.authorization_mode == "AGENT_POLICY"
        if status == "QUEUED":
            assert outbound.status == "FAILED" and outbound.error_code == "autopilot_removed"
            assert draft.current_revision_id != rev_id
            assert draft.current_revision.body_text == outbound.body_text
            assert not draft.current_revision.outbound_messages.exists()
            assert get("DurableJob").objects.get(idempotency_key="out-0").status == "COMPLETE"
        else:
            assert outbound.status == status and draft.current_revision_id == rev_id
    assert (
        not get("DurableJob")
        .objects.filter(kind="handle_conversation")
        .exclude(status="COMPLETE")
        .exists()
    )
    usage = get("ReplyAssistUsage").objects.get(owner=owner.id)
    assert (usage.requests, usage.input_tokens, usage.output_tokens) == (17, 100, 50)
    audits = get("AuditEvent").objects.filter(request_id="migration:0021:retire-autopilot")
    assert audits.filter(object_type="HandlingRun").count() == 4
    assert audits.filter(object_type="ConversationHandling").count() == 4
    assert audits.filter(object_type="OutboundMessage").count() == 4
    from inbox.models import Domain
    from inbox.services.legacy import redact_legacy_content

    redact_legacy_content(domain=Domain.objects.get(id=domain.id), cutoff=now + timedelta(days=1))
    assert audits.get(object_type="HandlingPolicy").metadata["record"]["knowledge"] == ""
    assert all(
        event.metadata["record"]["reply_brief"] == {}
        for event in audits.filter(object_type="HandlingRun")
    )
