from email import message_from_bytes, policy
from email.message import EmailMessage
from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from inbox.models import (
    Conversation,
    DraftApproval,
    Message,
    MessageRecipient,
    MessageReference,
    OutboundMessage,
    ReplyDraft,
)
from inbox.services.drafts import approve_exact_revision, create_authored_draft, send_exact_revision
from inbox.services.ingestion import (
    IngressEnvelope,
    RoutedDomain,
    _store_domain_message,
    process_delivery_notification,
)
from inbox.services.mime import parse_mime
from inbox.services.outbound import submit_outbound
from inbox.services.threading import match_conversation, reference_hash


@pytest.fixture
def outbound_factory(owner, inbound_message):
    MessageRecipient.objects.create(
        domain=inbound_message.domain,
        message=inbound_message,
        kind="ENVELOPE",
        address="onur@example.com",
        is_routing_recipient=True,
    )

    def create(authorization_mode=OutboundMessage.AuthorizationMode.DELEGATED_SCOPE):
        draft = create_authored_draft(
            message=inbound_message,
            owner=owner,
            subject="Re: Privacy request",
            body_text="Thank you.",
        )
        revision = draft.current_revision
        send = (
            approve_exact_revision
            if authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL
            else send_exact_revision
        )
        return send(
            draft=draft, revision_id=revision.id, content_hash=revision.content_hash, owner=owner
        )

    return create


def inbound_reply(outbound):
    message = EmailMessage()
    message["From"] = outbound.to_address
    message["To"] = outbound.from_address
    message["Subject"] = f"Re: {outbound.subject}"
    message["Message-ID"] = "<reply-message@example.net>"
    message["In-Reply-To"] = outbound.rfc_message_id
    message.set_content("Received, thank you.")
    return parse_mime(message.as_bytes())


@pytest.mark.django_db
def test_reply_mime_timeline_and_incoming_reply_share_a_conversation(
    owner, domain, outbound_factory
):
    outbound = outbound_factory()
    ses = Mock()
    ses.send_raw_email.return_value = {"MessageId": "reply-ses-1"}
    result = submit_outbound(outbound, ses_client=ses)
    assert result.status == OutboundMessage.Status.ACCEPTED
    raw = ses.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
    message = message_from_bytes(raw, policy=policy.default)
    assert str(message["From"]) == outbound.from_address
    assert str(message["To"]) == outbound.to_address
    assert message.get_content_type() == "text/plain"
    assert message["In-Reply-To"] == outbound.revision.draft.context_message.rfc_message_id
    assert message["References"] == message["In-Reply-To"]
    timeline = Message.objects.get(provider_message_id="reply-ses-1")
    assert timeline.recipients.get().address == outbound.to_address
    assert timeline.rfc_message_id == result.rfc_message_id == "reply-ses-1"
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
@pytest.mark.parametrize(
    "authorization_mode",
    [
        OutboundMessage.AuthorizationMode.OWNER_APPROVAL,
        OutboundMessage.AuthorizationMode.DELEGATED_SCOPE,
    ],
)
def test_reply_arriving_during_ses_submission_is_attached_to_reply_thread(
    owner, domain, outbound_factory, authorization_mode
):
    outbound = outbound_factory(authorization_mode=authorization_mode)

    def accept_with_early_reply(**kwargs):
        incoming = _store_domain_message(
            routed=RoutedDomain(domain, (outbound.from_address,), ()),
            parsed=inbound_reply(outbound),
            mail={"messageId": "early-reply-message"},
            receipt={"virusVerdict": {"status": "PASS"}},
            raw_key="test/early-reply.eml",
            attachment_keys=[],
        )
        assert incoming.conversation_id == outbound.conversation_id
        return {"MessageId": "early-reply-ses"}

    ses = Mock()
    ses.send_raw_email.side_effect = accept_with_early_reply
    assert submit_outbound(outbound, ses_client=ses).status == OutboundMessage.Status.ACCEPTED
    assert Conversation.objects.count() == 1
    assert Message.objects.count() == 3
    outbound.revision.draft.refresh_from_db()
    assert outbound.revision.draft.is_stale is True
    if authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL:
        assert DraftApproval.objects.get().invalidated_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("early_event", [False, True])
def test_ses_replaced_id_threads_replies_before_the_send_response(
    owner, domain, outbound_factory, early_event
):
    outbound = outbound_factory()
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
    assert Message.objects.count() == 3
    if not early_event:
        assert outbound.conversation.tags.get().normalized_name == "follow up"
        assert ReplyDraft.objects.get(context_message=incoming).conversation_id == (
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
def test_ses_wire_reference_updates_the_authoritative_timeline_id(owner, domain, outbound_factory):
    outbound = outbound_factory()
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
    owner, domain, outbound_factory
):
    outbound = outbound_factory()
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
    assert Message.objects.count() == 3
    assert Message.objects.get(provider_message_id="reply-before-unknown").conversation_id == (
        outbound.conversation_id
    )
    sent = Message.objects.get(provider_message_id="late-ses-id")
    assert sent.rfc_message_id == wire_id and sent.recipients.get().address == outbound.to_address
    ses.send_raw_email.assert_called_once()
