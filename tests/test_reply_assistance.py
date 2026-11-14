import json
import uuid
from unittest.mock import Mock

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from inbox.models import DurableJob, Message, OutboundMessage, ReplyAssistUsage, ReplyDraft, User
from inbox.services import reply_assist_model as model
from inbox.services.drafts import create_authored_draft
from inbox.services.reply_assistance import assist, context_token, evidence

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("operation", ["goals", "generate", "review"])
def test_assistance_returns_only_suggestions(
    client, owner, inbound_message, monkeypatch, operation
):
    conversation = inbound_message.conversation
    result = (
        {
            "goals": [
                {"label": "Accept", "instructions": "Accept the link exchange."},
                {"label": "Decline", "instructions": "Decline the link exchange."},
            ]
        }
        if operation == "goals"
        else {
            "subject": "Re: Exchange",
            "body_text": "Thank you for the proposal.",
            "notes": ["Confirm the target URL."],
        }
    )
    call = Mock(return_value=result)
    monkeypatch.setattr(model, "suggest", call)
    client.force_login(owner)
    response = client.post(
        reverse("conversation_reply_assist", args=[conversation.id]),
        {
            "operation": operation,
            "context_token": context_token(conversation),
            "subject": "Re: Exchange",
            "body_text": "My reply",
            "instructions": "Accept, ask for their URL.",
        },
    )
    assert response.status_code == 200
    for key, value in result.items():
        assert response.json()[key] == value
    supplied = call.call_args.args[2]
    assert supplied["body_text"] == "My reply" and supplied["owner_instructions"].startswith(
        "Accept"
    )
    assert "domain_knowledge" not in supplied and "policy" not in supplied
    assert not ReplyDraft.objects.exists() and not OutboundMessage.objects.exists()
    assert not DurableJob.objects.exists()


def test_context_changes_during_model_and_before_apply_are_rejected(inbound_message, monkeypatch):
    conversation = inbound_message.conversation
    token = context_token(conversation)

    def change(*args):
        inbound_message.text_body = "Updated thread"
        inbound_message.save()
        return {"subject": "Reply", "body_text": "Old context", "notes": []}

    monkeypatch.setattr(model, "suggest", change)
    with pytest.raises(ValidationError) as error:
        assist(conversation, operation="generate", token=token, instructions="Accept")
    assert error.value.code == "stale"
    with pytest.raises(ValidationError):
        assist(conversation, operation="validate", token=token)


def test_saved_draft_change_invalidates_suggestion_context(owner, inbound_message):
    conversation = inbound_message.conversation
    token = context_token(conversation)
    create_authored_draft(
        message=inbound_message, owner=owner, subject="Reply", body_text="External draft"
    )
    with pytest.raises(ValidationError):
        assist(conversation, operation="validate", token=token)


@pytest.mark.parametrize("change", ["quarantine", "purged", "readonly"])
def test_unavailable_context_never_reaches_model(inbound_message, monkeypatch, change):
    if change == "readonly":
        inbound_message.domain.status = "DISABLED"
        inbound_message.domain.save()
    elif change == "quarantine":
        inbound_message.is_quarantined = True
        inbound_message.save()
    else:
        inbound_message.normalized_purged_at = timezone.now()
        inbound_message.save()
    call = Mock()
    monkeypatch.setattr(model, "suggest", call)
    with pytest.raises(ValidationError):
        assist(
            inbound_message.conversation,
            operation="goals",
            token=context_token(inbound_message.conversation),
        )
    call.assert_not_called()


def test_context_is_owner_bound_and_endpoint_requires_csrf(client, owner, inbound_message):
    conversation = inbound_message.conversation
    url = reverse("conversation_reply_assist", args=[conversation.id])
    payload = {"operation": "goals", "context_token": context_token(conversation)}
    assert client.post(url, payload).status_code == 302
    outsider = User.objects.create_user(email="other@example.net", email_verified_at=timezone.now())
    client.force_login(outsider)
    assert client.post(url, payload).status_code == 404
    secure = Client(enforce_csrf_checks=True)
    secure.force_login(owner)
    assert secure.post(url, payload).status_code == 403


def test_failure_returns_503_and_manual_reply_remains_available(
    client, owner, inbound_message, monkeypatch
):
    client.force_login(owner)
    monkeypatch.setattr(model, "suggest", Mock(side_effect=model.ModelUnavailable("Try again")))
    response = client.post(
        reverse("conversation_reply_assist", args=[inbound_message.conversation_id]),
        {"operation": "goals", "context_token": context_token(inbound_message.conversation)},
    )
    assert response.status_code == 503
    page = client.get(reverse("conversation_detail", args=[inbound_message.conversation_id]))
    assert b"Send reply" in page.content and b"Save draft" in page.content
    assert not ReplyDraft.objects.exists()


def test_context_bounds_and_quarantined_history(inbound_message):
    conversation = inbound_message.conversation
    old = Message.objects.get(pk=inbound_message.id)
    old.pk = uuid.uuid4()
    old.provider_message_id = "old"
    old.rfc_message_id = "<old@example.net>"
    old.received_at = inbound_message.received_at - timezone.timedelta(days=1)
    old.text_body = "PRIVATE QUARANTINED CONTENT"
    old.is_quarantined = True
    old.save()
    result = evidence(conversation)
    assert result["unavailable_messages"] == 1
    assert "PRIVATE" not in json.dumps(result)
    inbound_message.text_body = "x" * 91000
    inbound_message.save()
    with pytest.raises(model.ModelUnavailable):
        assist(conversation, operation="goals", token=context_token(conversation))


@pytest.mark.parametrize(
    "operation,body,instructions",
    [("review", "", ""), ("generate", "", ""), ("archive", "text", "x")],
)
def test_invalid_writing_actions_have_no_effects(inbound_message, operation, body, instructions):
    with pytest.raises(ValidationError):
        assist(
            inbound_message.conversation,
            operation=operation,
            token=context_token(inbound_message.conversation),
            body_text=body,
            instructions=instructions,
        )
    assert not ReplyDraft.objects.exists()


def test_model_call_is_bounded_text_only_and_usage_is_retained(owner, settings, monkeypatch):
    settings.OPENAI_API_KEY = "synthetic-test"
    settings.REPLY_ASSIST_ENABLED = True
    result = {"goals": [{"label": "Accept", "instructions": "Accept the proposal."}]}
    response = Mock(status_code=200)
    response.json.return_value = {
        "output": [
            {"type": "function_call", "name": "reply_suggestion", "arguments": json.dumps(result)}
        ],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(model.httpx, "post", post)
    assert model.suggest(owner, "goals", {"messages": []}) == result
    request = post.call_args.kwargs["json"]
    assert request["store"] is False and request["parallel_tool_calls"] is False
    assert [tool["name"] for tool in request["tools"]] == ["reply_suggestion"]
    usage = ReplyAssistUsage.objects.get(owner=owner)
    assert usage.requests == 1 and usage.input_tokens == 20 and usage.output_tokens == 10
    post.side_effect = httpx.ReadTimeout("synthetic timeout")
    with pytest.raises(model.ModelUnavailable):
        model.suggest(owner, "review", {"body_text": "My draft"})
    usage.refresh_from_db()
    assert usage.requests == 2 and usage.input_tokens > 20
    settings.REPLY_ASSIST_DAILY_REQUESTS = 2
    with pytest.raises(model.ModelUnavailable, match="limit"):
        model.suggest(owner, "goals", {})
    assert post.call_count == 2


@pytest.mark.parametrize(
    "data",
    [
        {"goals": [{"label": "Accept", "instructions": "Accept"}] * 4},
        {"goals": [{"label": "Accept", "instructions": "Accept"}] * 2},
        {"goals": [], "action": "send"},
        {"subject": "Bad\nBcc: test@example.net", "body_text": "Body", "notes": []},
        {"subject": "Subject", "body_text": "Body", "notes": [False]},
    ],
)
def test_model_output_is_validated(owner, monkeypatch, data):
    monkeypatch.setattr(
        model,
        "request_model",
        Mock(
            return_value={
                "output": [
                    {
                        "type": "function_call",
                        "name": "reply_suggestion",
                        "arguments": json.dumps(data),
                    }
                ]
            }
        ),
    )
    with pytest.raises(model.ModelUnavailable):
        model.suggest(owner, "goals" if "goals" in data else "review", {})


def test_inbound_and_scheduler_never_start_conversation_work(domain, settings, monkeypatch):
    from inbox.services.ingestion import process_sqs_body
    from inbox.services.jobs import schedule_work
    from tests.test_ingestion import BUCKET, DELIVERY_TOPIC, INBOUND_TOPIC, FakeS3, sns_body
    from tests.test_mime_threading import raw_email

    settings.AWS_INGRESS_BUCKET = BUCKET
    settings.AWS_INBOUND_TOPIC_ARN = INBOUND_TOPIC
    settings.AWS_DELIVERY_TOPIC_ARN = DELIVERY_TOPIC
    call = Mock(side_effect=AssertionError("Unexpected background AI call"))
    monkeypatch.setattr(model, "request_model", call)
    assert process_sqs_body(sns_body(["support@example.com"]), s3_client=FakeS3(raw_email()))
    schedule_work()
    call.assert_not_called()
    assert not DurableJob.objects.filter(kind="handle_conversation").exists()
    assert not ReplyDraft.objects.exists() and not OutboundMessage.objects.exists()
    message = Message.objects.get()
    assert not message.conversation.tags.exists()
    assert message.conversation.archived_at is None


def test_historical_policy_provenance_cannot_authorize_new_send(owner, inbound_message):
    from inbox.services.outbound import submit_outbound

    draft = create_authored_draft(
        message=inbound_message, owner=owner, subject="Reply", body_text="Text"
    )
    outbound = OutboundMessage(
        domain=inbound_message.domain,
        conversation=inbound_message.conversation,
        revision=draft.current_revision,
        authorization_mode="AGENT_POLICY",
        from_address="me@example.com",
        to_address="sender@example.net",
        subject="Reply",
        body_text="Text",
        content_hash=draft.current_revision.content_hash,
        rfc_message_id="<legacy@example.com>",
    )
    with pytest.raises(ValidationError, match="removed"):
        outbound.full_clean()
    # Simulate a legacy queued record missed by an old worker during a rollback.
    outbound.save()
    provider = Mock()
    result = submit_outbound(outbound, ses_client=provider)
    assert result.status == "FAILED" and result.error_code == "send_authorization_revoked"
    provider.send_raw_email.assert_not_called()


def test_outbound_context_includes_uncertain_delivery_without_claiming_delivery(
    owner, inbound_message
):
    draft = create_authored_draft(
        message=inbound_message, owner=owner, subject="Reply", body_text="Previous reply"
    )
    outbound = OutboundMessage.objects.create(
        domain=inbound_message.domain,
        conversation=inbound_message.conversation,
        revision=draft.current_revision,
        authorization_mode="DELEGATED_SCOPE",
        status="UNKNOWN",
        subject="Reply",
        body_text="Previous reply",
        content_hash=draft.current_revision.content_hash,
        from_address="me@example.com",
        to_address="sender@example.net",
        rfc_message_id="<unknown@example.com>",
    )
    context = evidence(inbound_message.conversation)
    assert context["messages"][-1]["delivery_status"] == "UNKNOWN"
    assert context["messages"][-1]["body"] == outbound.body_text
