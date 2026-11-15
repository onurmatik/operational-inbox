import uuid
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from inbox.api import api
from inbox.models import APIToken, Conversation, DurableJob, Message, OutboundMessage, ReplyDraft
from inbox.services.drafts import create_authored_draft, resend_outbound
from inbox.services.outbound import submit_outbound

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("method", ["get", "post"])
def test_removed_compose_routes_have_no_side_effects(client, owner, domain, method):
    _, raw = APIToken.issue(owner=owner)
    client.force_login(owner)
    payload = {
        "from_address": "me@example.com",
        "to_address": "recipient@example.net",
        "subject": "Removed",
        "body_text": "Test",
        "idempotency_key": str(uuid.uuid4()),
    }
    for url in ("/app/compose/", f"/api/v1/domains/{domain.id}/outbound/compose"):
        response = getattr(client, method)(
            url,
            payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        assert response.status_code == 404
    assert not Conversation.objects.exists()
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not DurableJob.objects.exists()
    schema = api.get_openapi_schema()
    assert not any("compose" in path for path in schema["paths"])
    assert not any("Compose" in name for name in schema["components"]["schemas"])


def test_ui_only_offers_replies_and_receiving_setup(client, owner, domain):
    client.force_login(owner)
    for url in (
        reverse("inbox"),
        reverse("inbox") + "?folder=sent",
        reverse("domain_detail", args=[domain.id]),
    ):
        page = client.get(url)
        assert page.status_code == 200
        assert b"New email" not in page.content
        assert b"compose-dialog" not in page.content
        assert b"/app/compose/" not in page.content
    setup = client.get(reverse("domain_create"))
    assert b'value="send"' not in setup.content
    assert b"Receive &amp; reply" in setup.content
    assert setup.context["form"]["goal"].value() == "receive"


def test_api_rejects_send_only_domain_without_writing(client, owner, monkeypatch):
    inspect = Mock(side_effect=AssertionError("Invalid goals must not inspect DNS"))
    monkeypatch.setattr("inbox.services.domains.inspect_mx", inspect)
    client.force_login(owner)
    response = client.post(
        "/api/v1/domains",
        {
            "hostname": "outreach.example",
            "goal": "send",
        },
        content_type="application/json",
    )
    assert response.status_code == 422
    assert not owner.domains.exists()
    assert not DurableJob.objects.exists()
    inspect.assert_not_called()


@pytest.mark.parametrize("surface", ["ui", "api"])
@pytest.mark.parametrize("goal", ["receive", "both"])
def test_onboarding_keeps_receiving_and_optional_replies(client, owner, monkeypatch, surface, goal):
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    client.force_login(owner)
    payload = {"hostname": "receiving.example", "goal": goal, "setup_mode": "DIRECT_MX"}
    if surface == "ui":
        response = client.post(reverse("domain_create"), payload)
        assert response.status_code == 302
    else:
        response = client.post("/api/v1/domains", payload, content_type="application/json")
        assert response.status_code == 202
    domain = owner.domains.get()
    assert domain.receiving_enabled
    assert domain.sending_requested == (goal == "both")
    assert domain.inbound_routes.exists()


def test_draft_requires_an_inbound_context(owner, inbound_message):
    inbound_message.direction = Message.Direction.OUTBOUND
    inbound_message.save()
    with pytest.raises(ValidationError, match="inbound"):
        create_authored_draft(
            message=inbound_message, owner=owner, subject="Re: Test", body_text="Hi"
        )
    assert not ReplyDraft.objects.exists()
    with transaction.atomic(), pytest.raises(IntegrityError):
        ReplyDraft.objects.create(
            domain=inbound_message.domain,
            conversation=inbound_message.conversation,
        )


def test_draft_rejects_context_from_another_conversation(owner, inbound_message):
    other = Conversation.objects.create(
        domain=inbound_message.domain,
        first_message_at=inbound_message.received_at,
        last_message_at=inbound_message.received_at,
    )
    draft = ReplyDraft(
        domain=inbound_message.domain, conversation=other, context_message=inbound_message
    )
    with pytest.raises(ValidationError, match="same conversation"):
        draft.full_clean()


@pytest.mark.parametrize("changed", ["direction", "conversation", "domain"])
def test_worker_and_resend_reject_invalid_reply_context(owner, inbound_message, changed):
    from inbox.models import Domain, MessageRecipient
    from inbox.services.drafts import send_exact_revision

    domain = inbound_message.domain
    MessageRecipient.objects.create(
        domain=domain,
        message=inbound_message,
        kind="ENVELOPE",
        address="support@example.com",
        is_routing_recipient=True,
    )
    draft = create_authored_draft(
        message=inbound_message, owner=owner, subject="Re: Test", body_text="Hi"
    )
    revision = draft.current_revision
    outbound = send_exact_revision(
        draft=draft,
        revision_id=revision.id,
        content_hash=revision.content_hash,
        owner=owner,
    )
    if changed == "direction":
        Message.objects.filter(id=inbound_message.id).update(direction="OUTBOUND")
    elif changed == "conversation":
        other = Conversation.objects.create(
            domain=domain,
            first_message_at=inbound_message.received_at,
            last_message_at=inbound_message.received_at,
        )
        Message.objects.filter(id=inbound_message.id).update(conversation=other)
    else:
        other = Domain.objects.create(
            owner=owner,
            hostname="other.example",
            claim_expires_at=domain.claim_expires_at,
        )
        Message.objects.filter(id=inbound_message.id).update(domain=other)
    ses = Mock()
    result = submit_outbound(outbound, ses_client=ses)
    assert result.status == "FAILED" and result.error_code == "send_authorization_revoked"
    ses.send_raw_email.assert_not_called()
    with pytest.raises(ValidationError, match="inbound"):
        resend_outbound(result, owner=owner)
    assert OutboundMessage.objects.count() == 1
