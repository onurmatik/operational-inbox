from __future__ import annotations

import uuid
from email import message_from_bytes, policy

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    DraftApproval,
    DurableJob,
    MessageRecipient,
    OutboundMessage,
    ReplyDraft,
    User,
)
from inbox.services.drafts import current_reply_draft, revise_draft
from inbox.services.outbound import _raw_message, set_outbound_paused
from inbox.services.replies import save_reply_draft, send_manual_reply
from inbox.services.reply_assistance import context_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def reply_payload(inbound_message):
    MessageRecipient.objects.create(
        domain=inbound_message.domain,
        message=inbound_message,
        kind=MessageRecipient.Kind.TO,
        address="support@example.com",
        is_routing_recipient=True,
    )
    return {
        "context_message_id": inbound_message.id,
        "context_token": context_token(inbound_message.conversation),
        "idempotency_key": uuid.uuid4(),
        "subject": "Re: Privacy request",
        "body_text": "Hello,\n\nThis is my exact reply.\n",
    }


def test_manual_reply_queues_exact_owner_text_in_the_existing_thread(
    owner, conversation, inbound_message, reply_payload
):
    outbound = send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert outbound.status == "QUEUED"
    assert outbound.conversation_id == conversation.id
    assert outbound.body_text == reply_payload["body_text"]
    assert outbound.subject == reply_payload["subject"]
    assert outbound.from_address == "support@example.com"
    assert outbound.to_address == "reply@example.net"
    assert outbound.authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL
    assert not outbound.revision.is_agent_generated
    assert DraftApproval.objects.get().content_hash == outbound.content_hash
    assert DurableJob.objects.get().payload == {"outbound_id": str(outbound.id)}
    assert AuditEvent.objects.filter(event_type="outbound.replied").count() == 1
    wire = message_from_bytes(_raw_message(outbound), policy=policy.default)
    assert wire["In-Reply-To"] == inbound_message.rfc_message_id
    assert wire["References"] == inbound_message.rfc_message_id


@pytest.mark.parametrize("status", ["QUEUED", "UNKNOWN", "FAILED"])
def test_replay_returns_original_attempt_without_sending_again(
    owner, conversation, reply_payload, status
):
    first = send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    OutboundMessage.objects.filter(pk=first.pk).update(status=status)
    set_outbound_paused(owner, paused=True)
    repeat = send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert repeat.id == first.id and repeat.status == status
    assert OutboundMessage.objects.count() == DurableJob.objects.count() == 1
    assert ReplyDraft.objects.count() == DraftApproval.objects.count() == 1
    reply_payload["body_text"] = "Changed text"
    with pytest.raises(ValidationError, match="different content"):
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload)


@pytest.mark.parametrize(
    "block", ["quarantined", "purged", "new_inbound", "not_ready", "unverified", "paused", "quota"]
)
def test_rejected_reply_creates_no_send(
    owner, conversation, inbound_message, reply_payload, settings, block
):
    if block == "quarantined":
        inbound_message.is_quarantined = True
        inbound_message.save()
    elif block == "purged":
        inbound_message.normalized_purged_at = timezone.now()
        inbound_message.save()
    elif block == "new_inbound":
        inbound_message.pk = uuid.uuid4()
        inbound_message.provider_message_id = "newer-inbound"
        inbound_message.rfc_message_id = "<newer@example.net>"
        inbound_message.received_at = timezone.now()
        inbound_message.save()
    elif block in {"not_ready", "unverified"}:
        setattr(
            conversation.domain,
            "outbound_ready" if block == "not_ready" else "ownership_verified",
            False,
        )
        conversation.domain.save()
    elif block == "paused":
        set_outbound_paused(owner, paused=True)
    else:
        settings.OUTBOUND_MONTHLY_ACCOUNT_LIMIT = 1
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
        reply_payload["idempotency_key"] = uuid.uuid4()
    existing = OutboundMessage.objects.count()
    with pytest.raises(ValidationError):
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert OutboundMessage.objects.count() == existing
    assert ReplyDraft.objects.count() == existing


def test_queue_failure_rolls_back_reply(owner, conversation, reply_payload, monkeypatch):
    def fail_queue(**kwargs):
        raise RuntimeError("Queue unavailable")

    monkeypatch.setattr("inbox.services.replies.enqueue_job", fail_queue)
    with pytest.raises(RuntimeError, match="Queue unavailable"):
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()


def test_reply_form_and_ajax_send(client, owner, conversation, reply_payload):
    client.force_login(owner)
    page = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert b"Send reply" in page.content
    assert b"Write with AI" in page.content
    reply_payload["context_token"] = page.context["manual_reply_form"].initial["context_token"]
    response = client.post(
        reverse("conversation_reply", args=[conversation.id]),
        reply_payload,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200 and response.json()["status"] == "QUEUED"
    assert str(conversation.id) in response.json()["url"]


@pytest.mark.parametrize("ajax", [True, False])
def test_reply_errors_preserve_text(client, owner, conversation, reply_payload, ajax):
    client.force_login(owner)
    reply_payload["subject"] = "Re: Topic\nBcc: injected@example.net"
    response = client.post(
        reverse("conversation_reply", args=[conversation.id]),
        reply_payload,
        **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}),
    )
    assert response.status_code == 400
    if ajax:
        assert response.json()["message"]
    else:
        assert reply_payload["body_text"].encode() in response.content
    assert not OutboundMessage.objects.exists()


def test_another_owner_cannot_reply(client, conversation, reply_payload):
    outsider = User.objects.create_user(
        email="outsider@example.org", email_verified_at=timezone.now()
    )
    client.force_login(outsider)
    response = client.post(reverse("conversation_reply", args=[conversation.id]), reply_payload)
    assert response.status_code == 404
    with pytest.raises(ValidationError, match="owner"):
        send_manual_reply(conversation=conversation, owner=outsider, **reply_payload)
    assert not OutboundMessage.objects.exists()


def test_save_edit_and_send_share_one_draft(owner, conversation, reply_payload):
    data = {k: v for k, v in reply_payload.items() if k != "idempotency_key"}
    draft = save_reply_draft(conversation=conversation, owner=owner, **data)
    assert not OutboundMessage.objects.exists()
    assert current_reply_draft(conversation).id == draft.id
    reply_payload.update(
        draft_id=draft.id,
        expected_revision_id=draft.current_revision_id,
        context_token=context_token(conversation),
        body_text="Final owner text",
    )
    outbound = send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert outbound.revision.draft_id == draft.id
    assert outbound.revision.number == 2 and outbound.body_text == "Final owner text"
    assert ReplyDraft.objects.count() == 1
    assert current_reply_draft(conversation) is None
    assert (
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload).id == outbound.id
    )


def test_save_without_sending_readiness_and_detect_conflicts(owner, conversation, reply_payload):
    data = {k: v for k, v in reply_payload.items() if k != "idempotency_key"}
    conversation.domain.outbound_ready = False
    conversation.domain.save()
    draft = save_reply_draft(conversation=conversation, owner=owner, **data)
    token = context_token(conversation)
    old_revision = draft.current_revision_id
    revise_draft(
        draft=draft,
        owner=owner,
        subject="Other edit",
        body_text="From external agent",
        expected_revision_id=old_revision,
    )
    with pytest.raises(ValidationError) as error:
        save_reply_draft(
            conversation=conversation,
            owner=owner,
            **{
                **data,
                "draft_id": draft.id,
                "expected_revision_id": old_revision,
                "context_token": token,
            },
        )
    assert error.value.code == "stale"
    draft.refresh_from_db()
    assert draft.current_revision.body_text == "From external agent"
    assert not OutboundMessage.objects.exists()


def test_stale_draft_is_preserved_when_saving_for_current_inbound(
    owner, conversation, inbound_message, reply_payload
):
    data = {k: v for k, v in reply_payload.items() if k != "idempotency_key"}
    draft = save_reply_draft(conversation=conversation, owner=owner, **data)
    original = draft.current_revision_id
    ReplyDraft.objects.filter(id=draft.id).update(is_stale=True)
    inbound_message.pk = uuid.uuid4()
    inbound_message.provider_message_id = "new-message"
    inbound_message.rfc_message_id = "<new-message@example.net>"
    inbound_message.received_at = timezone.now()
    inbound_message.save()
    fresh = save_reply_draft(
        conversation=conversation,
        owner=owner,
        **{
            **data,
            "draft_id": draft.id,
            "expected_revision_id": original,
            "context_message_id": inbound_message.id,
            "context_token": context_token(conversation),
        },
    )
    draft.refresh_from_db()
    assert draft.is_stale and draft.current_revision_id == original
    assert fresh.id != draft.id and fresh.context_message_id == inbound_message.id
    assert not fresh.is_stale


def test_ui_save_returns_shared_reference_and_keeps_editor(
    client, owner, conversation, reply_payload
):
    client.force_login(owner)
    response = client.post(
        reverse("conversation_reply", args=[conversation.id]),
        {**reply_payload, "action": "save"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200
    assert response.json()["draft"]["id"] == str(current_reply_draft(conversation).id)
    assert not OutboundMessage.objects.exists()
    page = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert reply_payload["body_text"].encode() in page.content
    assert page.content.count(b"data-reply-form") == 1
    assert b"Take over" not in page.content and b"Review and send" not in page.content
