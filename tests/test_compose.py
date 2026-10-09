from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from threading import Barrier
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone

from inbox.models import (
    APIToken,
    AuditEvent,
    Conversation,
    Domain,
    DraftApproval,
    DurableJob,
    Message,
    MessageReference,
    OutboundMessage,
    ReplyDraft,
    User,
    compose_content_digest,
)
from inbox.services.compose import send_composed_email
from inbox.services.drafts import resend_outbound, revise_draft
from inbox.services.ingestion import (
    IngressEnvelope,
    RoutedDomain,
    _store_domain_message,
    process_delivery_notification,
)
from inbox.services.mime import parse_mime
from inbox.services.outbound import set_outbound_paused, submit_outbound
from inbox.services.retention import purge_retention
from inbox.services.threading import match_conversation, reference_hash


@pytest.fixture
def compose_payload():
    return {
        "from_address": "onur@example.com",
        "to_address": "contact@example.net",
        "subject": "Hello",
        "body_text": "A new conversation.\nThank you.",
        "idempotency_key": uuid.uuid4(),
    }


@pytest.mark.django_db
def test_compose_snapshots_and_queues_without_an_inbound_or_approval(
    owner, domain, compose_payload
):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert outbound.status == OutboundMessage.Status.QUEUED
    assert outbound.authorization_mode == OutboundMessage.AuthorizationMode.DELEGATED_SCOPE
    assert outbound.revision.draft.kind == ReplyDraft.Kind.COMPOSE
    assert outbound.revision.draft.context_message is None
    assert not Message.objects.exists()
    assert not DraftApproval.objects.exists()
    assert DurableJob.objects.get().payload == {"outbound_id": str(outbound.id)}
    assert AuditEvent.objects.get().event_type == "outbound.composed"
    assert outbound.content_hash == compose_content_digest(
        outbound.from_address, outbound.to_address, outbound.subject, outbound.body_text
    )
    with pytest.raises(ValidationError, match="snapshots"):
        revise_draft(draft=outbound.revision.draft, owner=owner, subject="Edit", body_text="Edit")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_address", "onur@another.example"),
        ("from_address", "Name <onur@example.com>"),
        ("to_address", "a@example.net,b@example.net"),
        ("to_address", "contact@example.net\r\nBcc: other@example.net"),
        ("subject", "Hello\nBcc: hidden@example.net"),
        ("subject", " " * 5),
        ("body_text", ""),
        ("body_text", "x" * 20001),
        ("idempotency_key", "invalid"),
    ],
)
def test_compose_invalid_input_leaves_no_send_records(owner, domain, compose_payload, field, value):
    compose_payload[field] = value
    with pytest.raises(ValidationError):
        send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert not Conversation.objects.exists()
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not DurableJob.objects.exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["outbound_ready", "ownership_verified"])
def test_compose_requires_a_verified_sending_domain(owner, domain, compose_payload, field):
    setattr(domain, field, False)
    domain.save(update_fields=[field])
    with pytest.raises(ValidationError, match="not ready"):
        send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert not Conversation.objects.exists()


@pytest.mark.django_db
def test_compose_rejects_another_owner(owner, domain, compose_payload):
    outsider = User.objects.create_user(email="other@example.net", email_verified_at=timezone.now())
    with pytest.raises(ValidationError, match="owner"):
        send_composed_email(domain=domain, owner=outsider, **compose_payload)
    assert not OutboundMessage.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["QUEUED", "FAILED", "UNKNOWN"])
def test_same_key_returns_existing_attempt_even_when_paused(owner, domain, compose_payload, status):
    first = send_composed_email(domain=domain, owner=owner, **compose_payload)
    OutboundMessage.objects.filter(id=first.id).update(status=status)
    set_outbound_paused(owner, paused=True)
    second = send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert second.id == first.id and second.status == status
    assert Conversation.objects.count() == DurableJob.objects.count() == 1
    assert AuditEvent.objects.filter(event_type="outbound.composed").count() == 1
    compose_payload["to_address"] = "different@example.net"
    with pytest.raises(ValidationError) as exc:
        send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert exc.value.error_list[0].code == "idempotency_conflict"


@pytest.mark.django_db
@override_settings(OUTBOUND_MONTHLY_ACCOUNT_LIMIT=1)
def test_compose_shares_limits_and_does_not_charge_an_idempotent_replay(
    owner, domain, compose_payload
):
    first = send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert send_composed_email(domain=domain, owner=owner, **compose_payload).id == first.id
    compose_payload["idempotency_key"] = uuid.uuid4()
    with pytest.raises(ValidationError) as exc:
        send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert exc.value.error_list[0].code == "outbound_monthly_limit"
    assert OutboundMessage.objects.count() == 1


@pytest.mark.django_db
def test_queue_failure_rolls_back_snapshot_and_conversation(
    owner, domain, compose_payload, monkeypatch
):
    monkeypatch.setattr(
        "inbox.services.compose.enqueue_job", Mock(side_effect=RuntimeError("queue"))
    )
    with pytest.raises(RuntimeError, match="queue"):
        send_composed_email(domain=domain, owner=owner, **compose_payload)
    assert not Conversation.objects.exists()
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_owner_send_records_approval_and_explicit_resend_keeps_the_snapshot(
    owner, domain, compose_payload
):
    outbound = send_composed_email(
        domain=domain,
        owner=owner,
        authorization_mode=OutboundMessage.AuthorizationMode.OWNER_APPROVAL,
        **compose_payload,
    )
    assert DraftApproval.objects.get().content_hash == outbound.content_hash
    outbound.status = OutboundMessage.Status.UNKNOWN
    outbound.save(update_fields=["status"])
    resend = resend_outbound(outbound, owner=owner)
    assert resend.revision_id == outbound.revision_id
    assert resend.parent_id == outbound.id and resend.attempt_number == 2
    assert resend.idempotency_key is None
    resend.to_address = "tampered@example.net"
    with pytest.raises(ValidationError):
        resend.full_clean()


def inbound_reply(outbound):
    message = EmailMessage()
    message["From"] = outbound.to_address
    message["To"] = outbound.from_address
    message["Subject"] = f"Re: {outbound.subject}"
    message["Message-ID"] = "<compose-reply@example.net>"
    message["In-Reply-To"] = outbound.rfc_message_id
    message.set_content("Received, thank you.")
    return parse_mime(message.as_bytes())


@pytest.mark.django_db
def test_compose_mime_timeline_and_incoming_reply_share_a_conversation(
    owner, domain, compose_payload
):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    ses = Mock()
    ses.send_raw_email.return_value = {"MessageId": "compose-ses-1"}
    result = submit_outbound(outbound, ses_client=ses)
    assert result.status == OutboundMessage.Status.ACCEPTED
    raw = ses.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
    message = message_from_bytes(raw, policy=policy.default)
    assert str(message["From"]) == compose_payload["from_address"]
    assert str(message["To"]) == compose_payload["to_address"]
    assert message.get_content_type() == "text/plain"
    assert message["In-Reply-To"] is None and message["References"] is None
    timeline = Message.objects.get(provider_message_id="compose-ses-1")
    assert timeline.recipients.get().address == outbound.to_address
    assert timeline.rfc_message_id == result.rfc_message_id == "compose-ses-1"
    assert timeline.references.filter(
        kind=MessageReference.Kind.MESSAGE_ID,
        value_hash=reference_hash(result.rfc_message_id),
    ).exists()
    assert (
        match_conversation(
            domain=domain,
            parsed=inbound_reply(outbound),
            envelope_recipients=[outbound.from_address],
        )
        == outbound.conversation
    )
    outbound.conversation.refresh_from_db()
    assert outbound.conversation.last_outbound_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["from_address", "to_address", "subject", "body_text"])
def test_compose_worker_rejects_changed_snapshot_before_ses(owner, domain, compose_payload, field):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    replacement = "changed@example.com" if field.endswith("address") else "Changed content"
    OutboundMessage.objects.filter(id=outbound.id).update(**{field: replacement})
    ses = Mock()
    result = submit_outbound(outbound, ses_client=ses)
    assert result.status == OutboundMessage.Status.FAILED
    assert result.error_code == "send_authorization_revoked"
    ses.send_raw_email.assert_not_called()
    assert not Message.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("authorization_mode", OutboundMessage.AuthorizationMode.values)
def test_reply_arriving_during_ses_submission_is_attached_to_compose(
    owner, domain, compose_payload, authorization_mode
):
    outbound = send_composed_email(
        domain=domain, owner=owner, authorization_mode=authorization_mode, **compose_payload
    )

    def accept_with_early_reply(**kwargs):
        incoming = _store_domain_message(
            routed=RoutedDomain(domain, (outbound.from_address,), ()),
            parsed=inbound_reply(outbound),
            mail={"messageId": "early-compose-reply"},
            receipt={"virusVerdict": {"status": "PASS"}},
            raw_key="test/early-reply.eml",
            attachment_keys=[],
        )
        assert incoming.conversation_id == outbound.conversation_id
        return {"MessageId": "early-compose-ses"}

    ses = Mock()
    ses.send_raw_email.side_effect = accept_with_early_reply
    assert submit_outbound(outbound, ses_client=ses).status == OutboundMessage.Status.ACCEPTED
    assert Conversation.objects.count() == 1
    assert Message.objects.count() == 2
    outbound.revision.draft.refresh_from_db()
    assert outbound.revision.draft.is_stale is False
    if authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL:
        assert DraftApproval.objects.get().invalidated_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("early_event", [False, True])
def test_ses_replaced_id_threads_replies_before_the_send_response(
    owner, domain, compose_payload, early_event
):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    wire_id = "<ses-assigned-early@us-east-1.amazonses.com>"
    message = EmailMessage()
    message["From"] = outbound.to_address
    message["To"] = outbound.from_address
    message["Subject"] = "Re: Hello"
    message["Message-ID"] = "<reply-to-ses@example.net>"
    message["In-Reply-To"] = wire_id
    message.set_content("Received.")

    def accept_with_wire_reply(**kwargs):
        if early_event:
            assert process_delivery_notification(
                IngressEnvelope(
                    sns_message_id="early-tagged-send",
                    topic_arn="test",
                    payload_digest="",
                    notification={
                        "eventType": "Send",
                        "mail": {
                            "messageId": "ses-assigned-early",
                            "tags": {"outbound_id": [str(outbound.id)]},
                            "commonHeaders": {"messageId": wire_id},
                        },
                    },
                )
            )
        incoming = _store_domain_message(
            routed=RoutedDomain(domain, (outbound.from_address,), ()),
            parsed=parse_mime(message.as_bytes()),
            mail={"messageId": "wire-early-reply"},
            receipt={"virusVerdict": {"status": "PASS"}},
            raw_key="test/early-wire-reply.eml",
            attachment_keys=[],
        )
        if early_event:
            assert incoming.conversation_id == outbound.conversation_id
        else:
            incoming.conversation.tags.create(
                domain=domain, name="Follow up", normalized_name="follow up"
            )
            ReplyDraft.objects.create(
                domain=domain,
                conversation=incoming.conversation,
                context_message=incoming,
            )
        return {"MessageId": "ses-assigned-early"}

    ses = Mock()
    ses.send_raw_email.side_effect = accept_with_wire_reply
    result = submit_outbound(outbound, ses_client=ses)
    assert result.status == OutboundMessage.Status.ACCEPTED
    assert Conversation.objects.count() == 1
    incoming = Message.objects.get(provider_message_id="wire-early-reply")
    assert incoming.conversation_id == outbound.conversation_id
    assert Message.objects.count() == 2
    if not early_event:
        assert outbound.conversation.tags.get().normalized_name == "follow up"
        assert ReplyDraft.objects.get(kind=ReplyDraft.Kind.REPLY).conversation_id == (
            outbound.conversation_id
        )
    assert (
        match_conversation(
            domain=domain,
            parsed=parse_mime(message.as_bytes()),
            envelope_recipients=[outbound.from_address],
        )
        == outbound.conversation
    )


@pytest.mark.django_db
def test_ses_wire_reference_updates_the_authoritative_timeline_id(owner, domain, compose_payload):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    ses = Mock()
    ses.send_raw_email.return_value = {"MessageId": "ses-final-id"}
    submit_outbound(outbound, ses_client=ses)
    wire_id = "<ses-final-id@email.amazonses.com>"
    reply = EmailMessage()
    reply["From"] = outbound.to_address
    reply["To"] = outbound.from_address
    reply["In-Reply-To"] = wire_id
    reply.set_content("Received.")
    assert (
        match_conversation(
            domain=domain,
            parsed=parse_mime(reply.as_bytes()),
            envelope_recipients=[outbound.from_address],
        )
        == outbound.conversation
    )
    outbound.refresh_from_db()
    assert outbound.rfc_message_id == wire_id
    timeline = Message.objects.get(provider_message_id="ses-final-id")
    assert timeline.rfc_message_id == wire_id
    assert timeline.references.filter(value_hash=reference_hash(wire_id)).exists()


@pytest.mark.django_db
def test_late_delivery_event_records_unknown_send_and_reconciles_early_reply(
    owner, domain, compose_payload
):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    wire_id = "<late-ses-id@email.amazonses.com>"
    reply = EmailMessage()
    reply["From"] = outbound.to_address
    reply["To"] = outbound.from_address
    reply["In-Reply-To"] = wire_id
    reply.set_content("Received.")

    def uncertain_acceptance(**kwargs):
        _store_domain_message(
            routed=RoutedDomain(domain, (outbound.from_address,), ()),
            parsed=parse_mime(reply.as_bytes()),
            mail={"messageId": "reply-before-unknown"},
            receipt={"virusVerdict": {"status": "PASS"}},
            raw_key="test/unknown-reply.eml",
            attachment_keys=[],
        )
        raise ReadTimeoutError(endpoint_url="https://email.us-east-1.amazonaws.com")

    ses = Mock()
    ses.send_raw_email.side_effect = uncertain_acceptance
    assert submit_outbound(outbound, ses_client=ses).status == OutboundMessage.Status.UNKNOWN
    envelope = IngressEnvelope(
        sns_message_id="late-delivery",
        topic_arn="test",
        payload_digest="",
        notification={
            "eventType": "Delivery",
            "mail": {
                "messageId": "late-ses-id",
                "tags": {"outbound_id": [str(outbound.id)]},
                "commonHeaders": {"messageId": wire_id},
            },
        },
    )
    assert process_delivery_notification(envelope) is True
    assert process_delivery_notification(envelope) is True
    outbound.refresh_from_db()
    assert outbound.status == OutboundMessage.Status.DELIVERED
    assert OutboundMessage.objects.count() == 1
    assert Conversation.objects.count() == 1
    assert Message.objects.count() == 2
    assert Message.objects.get(provider_message_id="reply-before-unknown").conversation_id == (
        outbound.conversation_id
    )
    sent = Message.objects.get(provider_message_id="late-ses-id")
    assert sent.rfc_message_id == wire_id and sent.recipients.get().address == outbound.to_address
    ses.send_raw_email.assert_called_once()


@pytest.mark.django_db
def test_compose_snapshot_addresses_are_redacted_with_retention(owner, domain, compose_payload):
    outbound = send_composed_email(domain=domain, owner=owner, **compose_payload)
    now = timezone.now() + timedelta(days=400)
    purge_retention(now=now, s3_client=Mock())
    outbound.refresh_from_db()
    outbound.revision.refresh_from_db()
    assert outbound.from_address == outbound.revision.from_address == "redacted@invalid.local"
    assert outbound.to_address == outbound.revision.to_address == "redacted@invalid.local"
    assert outbound.body_text == outbound.revision.body_text == ""
    assert outbound.content_hash == outbound.revision.content_hash
    assert outbound.status == OutboundMessage.Status.FAILED


@pytest.mark.django_db
def test_compose_api_accepts_bearer_and_hides_snapshots_from_reply_tools(
    client, owner, domain, compose_payload
):
    _, raw = APIToken.issue(owner=owner)
    payload = {key: str(value) for key, value in compose_payload.items()}
    response = client.post(
        f"/api/v1/domains/{domain.id}/outbound/compose",
        data=payload,
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert response.status_code == 202
    outbound = OutboundMessage.objects.get(id=response.json()["outbound_id"])
    assert response.json()["conversation_id"] == str(outbound.conversation_id)
    client.force_login(owner)
    assert (
        client.get(f"/api/v1/domains/{domain.id}/drafts/{outbound.revision.draft_id}").status_code
        == 404
    )
    assert client.get(f"/api/v1/domains/{domain.id}/conversations").json()["items"] == []


@pytest.mark.django_db
def test_compose_api_validation_and_idempotency_conflict(client, owner, domain, compose_payload):
    client.force_login(owner)
    url = f"/api/v1/domains/{domain.id}/outbound/compose"
    payload = {key: str(value) for key, value in compose_payload.items()}
    first = client.post(url, payload, content_type="application/json")
    repeated = client.post(url, payload, content_type="application/json")
    assert first.json() == repeated.json()
    payload["body_text"] = "Changed"
    conflict = client.post(url, payload, content_type="application/json")
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    payload["to_address"] = "invalid"
    invalid = client.post(url, payload, content_type="application/json")
    assert invalid.status_code == 400 and "to_address" in invalid.json()["fields"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("blocked", "expected_status", "expected_code"),
    [
        ("paused", 409, "outbound_paused"),
        ("not_ready", 409, "outbound_not_ready"),
        ("unverified", 409, "outbound_not_ready"),
        ("quota", 429, "quota_exhausted"),
        ("foreign_from", 400, "validation_error"),
        ("invalid_to", 400, "validation_error"),
        ("missing_subject", 422, "validation_error"),
        ("cross_tenant", 404, "not_found"),
    ],
)
def test_compose_api_failures_do_not_create_partial_sends(
    client, owner, domain, compose_payload, settings, blocked, expected_status, expected_code
):
    client.force_login(owner)
    payload = {key: str(value) for key, value in compose_payload.items()}
    url = f"/api/v1/domains/{domain.id}/outbound/compose"
    existing_count = 0
    if blocked == "paused":
        set_outbound_paused(owner, paused=True)
    elif blocked in {"not_ready", "unverified"}:
        field = "outbound_ready" if blocked == "not_ready" else "ownership_verified"
        setattr(domain, field, False)
        domain.save(update_fields=[field])
    elif blocked == "quota":
        settings.OUTBOUND_MONTHLY_ACCOUNT_LIMIT = 1
        send_composed_email(domain=domain, owner=owner, **compose_payload)
        existing_count = 1
        payload["idempotency_key"] = str(uuid.uuid4())
    elif blocked == "foreign_from":
        payload["from_address"] = "onur@other.example"
    elif blocked == "invalid_to":
        payload["to_address"] = "contact@example.net\r\nBcc: hidden@example.net"
    elif blocked == "missing_subject":
        del payload["subject"]
    else:
        outsider = User.objects.create_user(
            email="api-outsider@example.net", email_verified_at=timezone.now()
        )
        client.force_login(outsider)
    response = client.post(url, payload, content_type="application/json")
    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert Conversation.objects.count() == existing_count
    assert ReplyDraft.objects.count() == OutboundMessage.objects.count() == existing_count
    assert DurableJob.objects.count() == existing_count
    if blocked == "quota":
        assert response.json()["resource"] == "outbound_replies"
        assert response.json()["period"] == "calendar_month"
        assert response.json()["reset_at"] is not None


@pytest.mark.django_db(transaction=True)
def test_concurrent_compose_calls_create_one_attempt(owner, domain, compose_payload):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock concurrency is verified on PostgreSQL.")
    barrier = Barrier(2)

    def send():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return send_composed_email(
                owner=User.objects.get(id=owner.id),
                domain=Domain.objects.get(id=domain.id),
                **compose_payload,
            ).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: send(), range(2)))
    assert attempts[0] == attempts[1]
    assert OutboundMessage.objects.count() == DurableJob.objects.count() == 1
