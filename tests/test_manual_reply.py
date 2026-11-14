from __future__ import annotations

import uuid
from email import message_from_bytes, policy

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    ConversationHandling,
    DraftApproval,
    DurableJob,
    MessageRecipient,
    OutboundMessage,
    ReplyDraft,
    User,
)
from inbox.services.handling import set_handling
from inbox.services.outbound import _raw_message, set_outbound_paused
from inbox.services.replies import send_manual_reply

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
    state = ConversationHandling.objects.get(conversation=conversation)
    assert state.taken_over and state.status == "manual"
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


def test_send_stops_pending_agent_and_followup(owner, conversation, reply_payload):
    state = set_handling(conversation, owner=owner, action="handoff")
    state.followup_at = timezone.now()
    state.save()
    generation = state.generation
    send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    state.refresh_from_db()
    assert state.taken_over and state.generation > generation
    assert state.followup_at is None


@pytest.mark.parametrize(
    "block", ["quarantined", "purged", "new_inbound", "not_ready", "unverified", "paused", "quota"]
)
def test_rejected_reply_creates_no_send_or_takeover(
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
    assert ConversationHandling.objects.count() == existing


def test_queue_failure_rolls_back_reply_and_takeover(
    owner, conversation, reply_payload, monkeypatch
):
    def fail_queue(**kwargs):
        raise RuntimeError("Queue unavailable")

    monkeypatch.setattr("inbox.services.replies.enqueue_job", fail_queue)
    with pytest.raises(RuntimeError, match="Queue unavailable"):
        send_manual_reply(conversation=conversation, owner=owner, **reply_payload)
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not ConversationHandling.objects.exists()


def test_reply_form_and_ajax_send(client, owner, conversation, reply_payload):
    client.force_login(owner)
    page = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert b"Send reply" in page.content
    assert b"Reply with agent" in page.content
    response = client.post(
        reverse("conversation_reply", args=[conversation.id]),
        reply_payload,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert response.status_code == 200 and response.json()["status"] == "QUEUED"
    assert str(conversation.id) in response.json()["url"]


def test_active_agent_requires_takeover_before_manual_editor(
    client, owner, conversation, reply_payload
):
    client.force_login(owner)
    set_handling(conversation, owner=owner, action="handoff")
    page = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert b"Take over" in page.content
    assert b"data-manual-reply-form" not in page.content
    page = client.post(
        reverse("conversation_handling", args=[conversation.id]),
        {"action": "takeover"},
        follow=True,
    )
    assert b"data-manual-reply-form" in page.content


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
