from __future__ import annotations

from email.message import EmailMessage

import pytest

from inbox.models import MessageRecipient, MessageReference
from inbox.services.mime import parse_mime
from inbox.services.threading import match_conversation, reference_hash


def raw_email(*, subject="Hello", body="Plain body", html=None, attachment=False) -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.net>"
    message["To"] = "privacy@example.org"
    message["Subject"] = subject
    message["Message-ID"] = "<new@example.net>"
    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")
    if attachment:
        message.add_attachment(
            b"safe test bytes",
            maintype="application",
            subtype="octet-stream",
            filename="../report.txt",
        )
    return message.as_bytes()


def test_mime_parser_sanitizes_active_and_external_content():
    parsed = parse_mime(
        raw_email(
            html=(
                "<p>Hello <strong>team</strong></p><script>alert(1)</script>"
                '<img src="https://tracker.example/pixel"><a href="javascript:alert(2)">bad</a>'
            ),
            attachment=True,
        )
    )
    assert "<script" not in parsed.html_body
    assert "<img" not in parsed.html_body
    assert "javascript:" not in parsed.html_body
    assert "<strong>team</strong>" in parsed.html_body
    assert parsed.attachments[0].filename == "report.txt"


@pytest.mark.django_db
def test_threading_requires_reference_and_participant_overlap(
    project, conversation, inbound_message
):
    MessageReference.objects.create(
        domain=project,
        message=inbound_message,
        kind=MessageReference.Kind.MESSAGE_ID,
        value_hash=reference_hash(inbound_message.rfc_message_id),
    )
    MessageRecipient.objects.create(
        domain=project,
        message=inbound_message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address="privacy@example.org",
        is_routing_recipient=True,
    )
    reply = EmailMessage()
    reply["From"] = "sender@example.net"
    reply["To"] = "privacy@example.org"
    reply["Subject"] = "Re: Privacy request"
    reply["Message-ID"] = "<reply@example.net>"
    reply["In-Reply-To"] = inbound_message.rfc_message_id
    reply.set_content("Following up")
    matched = match_conversation(
        domain=project,
        parsed=parse_mime(reply.as_bytes()),
        envelope_recipients=["privacy@example.org"],
    )
    assert matched == conversation

    outsider = EmailMessage()
    outsider["From"] = "unrelated@elsewhere.test"
    outsider["To"] = "legal@different.test"
    outsider["Subject"] = "Re: Privacy request"
    outsider["Message-ID"] = "<outsider@example.net>"
    outsider["In-Reply-To"] = inbound_message.rfc_message_id
    outsider.set_content("Not a participant")
    assert (
        match_conversation(
            domain=project,
            parsed=parse_mime(outsider.as_bytes()),
            envelope_recipients=["legal@different.test"],
        )
        is None
    )
