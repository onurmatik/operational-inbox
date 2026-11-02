from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    ConversationHandling,
    HandlingPolicy,
    HandlingRun,
    HandlingUsage,
    Message,
    MessageRecipient,
    OutboundMessage,
    User,
)
from inbox.services import handling_model
from inbox.services.handling import (
    authorization_error,
    on_inbound,
    run_handling,
    schedule_handling,
    set_handling,
)
from inbox.services.handling_policy import (
    account_policy,
    effective_policy,
    resolve_action,
    save_policy,
)
from inbox.services.outbound import set_outbound_paused, submit_outbound

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready(inbound_message, monkeypatch):
    message = inbound_message
    message.reply_to_address = message.from_address
    message.subject = "Incorrect Charge"
    message.text_body = "I have a $48 charge on October 28. I did not purchase anything."
    message.save()
    MessageRecipient.objects.create(
        domain=message.domain,
        message=message,
        kind="ENVELOPE",
        address="support@example.com",
        is_routing_recipient=True,
    )
    monkeypatch.setattr(
        handling_model,
        "classify",
        Mock(
            return_value={
                "type": "billing",
                "risk": "low",
                "confidence": 0.99,
                "missing_information": 0.98,
                "automated": 0.01,
            }
        ),
    )
    monkeypatch.setattr(
        handling_model,
        "propose",
        Mock(
            return_value={
                "action": "reply",
                "body": "Which account email and merchant description appear on the charge?",
                "waiting_for_reply": True,
                "add_tags": [],
                "remove_tags": [],
            }
        ),
    )
    monkeypatch.setattr(handling_model, "safe_reply", Mock(return_value=True))
    return message


def run_initial(message):
    state = set_handling(message.conversation, owner=message.domain.owner, action="handoff")
    run = HandlingRun.objects.get(conversation=message.conversation, generation=state.generation)
    run_handling(str(run.id))
    state.refresh_from_db()
    run.refresh_from_db()
    return state, run


def test_missing_billing_details_asks_without_claiming_refund(ready):
    state, run = run_initial(ready)
    outbound = OutboundMessage.objects.get(agent_run=run)
    assert state.classification["type"] == "billing"
    assert state.status == "waiting"
    assert outbound.authorization_mode == "AGENT_POLICY"
    assert "account email" in outbound.body_text
    assert not authorization_error(outbound)
    assert ready.conversation.archived_at is None


def test_duplicate_job_cannot_duplicate_reply(ready):
    _, run = run_initial(ready)
    run_handling(str(run.id))
    assert OutboundMessage.objects.count() == 1
    assert run.conversation.reply_drafts.count() == 1


@pytest.mark.parametrize("change", ["takeover", "policy", "inbound", "tag", "snapshot"])
def test_pending_send_is_revoked(ready, change):
    _, run = run_initial(ready)
    outbound = OutboundMessage.objects.get(agent_run=run)
    if change == "takeover":
        set_handling(ready.conversation, owner=ready.domain.owner, action="takeover")
    elif change == "policy":
        p = account_policy(ready.domain.owner)
        p.mode = "draft"
        save_policy(p)
    elif change == "inbound":
        message = Message.objects.create(
            domain=ready.domain,
            conversation=ready.conversation,
            direction="INBOUND",
            provider_message_id="new-message",
            from_address=ready.from_address,
            received_at=timezone.now(),
            virus_verdict="PASS",
        )
        on_inbound(message)
    elif change == "tag":
        from inbox.services.tags import add_conversation_tag

        add_conversation_tag(ready.conversation, "manual-review")
    else:
        outbound.body_text = "Refund completed"
        outbound.save()
    client = Mock()
    submit_outbound(outbound, ses_client=client)
    client.send_raw_email.assert_not_called()
    outbound.refresh_from_db()
    assert outbound.error_code == "send_authorization_revoked"


def test_takeover_during_model_call_fences_result(ready):
    handling_model.propose.side_effect = lambda *a, **k: (
        set_handling(ready.conversation, owner=ready.domain.owner, action="takeover")
        and {
            "action": "reply",
            "body": "Hello",
            "waiting_for_reply": False,
            "add_tags": [],
            "remove_tags": [],
        }
    )
    state, run = run_initial(ready)
    assert state.taken_over
    assert run.status == "superseded"
    assert not OutboundMessage.objects.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_quarantined", True),
        ("spf_verdict", "FAIL"),
        ("reply_to_address", "attacker@example.net"),
    ],
)
def test_security_blocks_sending_and_quarantine_blocks_model(ready, field, value):
    setattr(ready, field, value)
    ready.save()
    state, _ = run_initial(ready)
    assert state.status == "review"
    if field == "is_quarantined":
        handling_model.classify.assert_not_called()
    else:
        handling_model.classify.assert_called_once()
    handling_model.propose.assert_not_called()


def test_automated_message_never_replies(ready):
    ready.automation_headers = {"auto_submitted": "auto-replied"}
    ready.save()
    state, _ = run_initial(ready)
    assert state.status == "done"
    handling_model.propose.assert_not_called()
    assert not OutboundMessage.objects.exists()


@pytest.mark.parametrize("risk,confidence", [("high", 0.99), ("unknown", 0.99), ("low", 0.5)])
def test_risk_and_uncertainty_require_review(ready, risk, confidence):
    handling_model.classify.return_value.update(risk=risk, confidence=confidence)
    state, _ = run_initial(ready)
    assert state.status == "review"
    assert not OutboundMessage.objects.exists()


def test_medium_risk_only_drafts(ready):
    handling_model.classify.return_value["risk"] = "medium"
    state, run = run_initial(ready)
    assert state.status == "review" and run.draft_id
    assert not OutboundMessage.objects.exists()


def test_reply_guard_rejects_unsupported_action(ready):
    handling_model.safe_reply.return_value = False
    state, _ = run_initial(ready)
    assert state.status == "review"
    assert not OutboundMessage.objects.exists()


def test_pause_keeps_draft(ready):
    set_outbound_paused(ready.domain.owner, paused=True)
    state, run = run_initial(ready)
    assert state.status == "review" and run.draft_id
    assert not OutboundMessage.objects.exists()


def test_one_reminder_then_review(ready):
    state, initial = run_initial(ready)
    now = timezone.now()
    first = OutboundMessage.objects.get(agent_run=initial)
    first.status = "ACCEPTED"
    first.accepted_at = now - timedelta(hours=73)
    first.save()
    state.followup_at = now - timedelta(hours=1)
    state.save()
    assert schedule_handling(now) == 1
    followup = HandlingRun.objects.get(phase="followup")
    run_handling(str(followup.id))
    reminder = OutboundMessage.objects.get(agent_run=followup)
    reminder.status = "ACCEPTED"
    reminder.accepted_at = now - timedelta(hours=73)
    reminder.save()
    state.refresh_from_db()
    state.followup_at = now - timedelta(hours=1)
    state.save()
    assert schedule_handling(now) == 1
    final = HandlingRun.objects.get(phase="escalate")
    run_handling(str(final.id))
    state.refresh_from_db()
    assert state.status == "review"
    assert OutboundMessage.objects.count() == 2
    assert schedule_handling(now + timedelta(days=20)) == 0


@pytest.mark.parametrize("delivery", ["UNKNOWN", "FAILED", "BOUNCED", "COMPLAINED"])
def test_failed_or_uncertain_delivery_never_followed_up(ready, delivery):
    state, run = run_initial(ready)
    OutboundMessage.objects.filter(agent_run=run).update(status=delivery)
    state.followup_at = timezone.now() - timedelta(seconds=1)
    state.save()
    assert schedule_handling() == 0
    state.refresh_from_db()
    assert state.status == "review"
    assert OutboundMessage.objects.count() == 1


def test_new_mails_only_and_manual_history_handoff(ready):
    account_policy(ready.domain.owner)
    assert schedule_handling() == 0
    assert not ConversationHandling.objects.exists()
    state, _ = run_initial(ready)
    assert state.status == "waiting"


def test_takeover_persists_for_new_inbound(ready):
    set_handling(ready.conversation, owner=ready.domain.owner, action="takeover")
    msg = Message.objects.create(
        domain=ready.domain,
        conversation=ready.conversation,
        direction="INBOUND",
        provider_message_id="another",
        received_at=timezone.now(),
        from_address="sender@example.net",
    )
    on_inbound(msg)
    assert ConversationHandling.objects.get().status == "manual"
    assert not HandlingRun.objects.exists()


def test_domain_rules_override_account_with_and_or_conditions(ready):
    account = account_policy(ready.domain.owner)
    account.rules = [{"name": "fallback", "action": "review"}]
    save_policy(account)
    domain = HandlingPolicy(
        owner=ready.domain.owner,
        domain=ready.domain,
        mode="inherit",
        rules=[
            {
                "name": "specific",
                "types": ["billing", "support"],
                "tags": ["VIP", "known"],
                "risks": ["low"],
                "action": "auto",
            }
        ],
    )
    save_policy(domain)
    p = effective_policy(ready.domain)
    classification = {"type": "billing", "risk": "low", "confidence": 0.99}
    assert resolve_action(p, classification, ["vip"])[0] == "auto"
    assert resolve_action(p, classification, ["other"])[0] == "review"


def test_tenant_cannot_handoff_or_modify_policy(ready):
    other = User.objects.create_user(email="other@example.org", password="test-password")
    with pytest.raises(ValidationError):
        set_handling(ready.conversation, owner=other, action="handoff")
    with pytest.raises(ValidationError):
        save_policy(HandlingPolicy(owner=other, domain=ready.domain))


def test_model_failure_is_visible(ready):
    handling_model.classify.side_effect = handling_model.ModelUnavailable(
        "Agent API is unavailable."
    )
    state, _ = run_initial(ready)
    assert state.status == "review"
    assert "unavailable" in state.reason


def test_expired_lease_can_retry_without_duplicate(ready):
    state = set_handling(ready.conversation, owner=ready.domain.owner, action="handoff")
    state.lease_until = timezone.now() - timedelta(seconds=1)
    state.save()
    run_handling(str(HandlingRun.objects.get().id))
    assert OutboundMessage.objects.count() == 1


def test_active_lease_blocks_second_worker(ready):
    state = set_handling(ready.conversation, owner=ready.domain.owner, action="handoff")
    state.lease_until = timezone.now() + timedelta(minutes=2)
    state.save()
    with pytest.raises(handling_model.ModelRetryable):
        run_handling(str(HandlingRun.objects.get().id))
    handling_model.classify.assert_not_called()


def test_settings_and_conversation_ui(client, ready):
    client.force_login(ready.domain.owner)
    response = client.get(reverse("agent_settings"))
    assert response.status_code == 200
    assert b"Account defaults" in response.content
    response = client.get(reverse("conversation_detail", args=[ready.conversation_id]))
    assert b"Hand to agent" in response.content
    run_initial(ready)
    response = client.get(reverse("conversation_detail", args=[ready.conversation_id]))
    assert b"Take over" in response.content and b"Billing" in response.content


def test_save_settings_with_ordered_rules(client, ready):
    client.force_login(ready.domain.owner)
    response = client.post(
        reverse("agent_settings"),
        {
            "mode": "draft",
            "followup_enabled": "on",
            "followup_hours": "48",
            "rules-TOTAL_FORMS": "1",
            "rules-INITIAL_FORMS": "0",
            "rules-MIN_NUM_FORMS": "0",
            "rules-MAX_NUM_FORMS": "30",
            "rules-0-name": "Billing",
            "rules-0-types": ["billing"],
            "rules-0-risks": ["low"],
            "rules-0-tags": "VIP",
            "rules-0-add_tags": "billing",
            "rules-0-action": "draft",
            "rules-0-ORDER": "1",
        },
    )
    assert response.status_code == 302
    policy = account_policy(ready.domain.owner)
    assert policy.mode == "draft"
    assert policy.rules[0]["tags"] == ["VIP"]


def test_budget_reserves_before_request(owner, monkeypatch, settings):
    settings.OPENAI_API_KEY = "test-key"
    settings.HANDLING_DAILY_REQUESTS = 1
    response = Mock(status_code=200)
    response.json.return_value = {"usage": {"input_tokens": 3, "output_tokens": 0}, "answers": []}
    post = Mock(return_value=response)
    monkeypatch.setattr(handling_model.httpx, "post", post)
    handling_model.request_model(owner, "decisions", {"input": "test"})
    with pytest.raises(handling_model.ModelUnavailable, match="limit"):
        handling_model.request_model(owner, "decisions", {"input": "test"})
    assert post.call_count == 1
    assert HandlingUsage.objects.get().input_tokens == 3


def test_adapter_treats_refusal_as_review(owner, monkeypatch):
    monkeypatch.setattr(
        handling_model, "request_model", lambda *a: {"answers": [{"name": "x", "type": "refusal"}]}
    )
    with pytest.raises(handling_model.ModelUnavailable):
        handling_model.decisions(owner, {}, [{"name": "x", "type": "predicate"}])


def test_owner_adopts_pending_reply_without_duplicate(ready):
    from inbox.services.drafts import approve_exact_revision

    _, run = run_initial(ready)
    original = OutboundMessage.objects.get(agent_run=run)
    revision = original.revision
    adopted = approve_exact_revision(
        draft=revision.draft,
        revision_id=revision.id,
        content_hash=revision.content_hash,
        owner=ready.domain.owner,
    )
    assert adopted.pk == original.pk
    assert adopted.authorization_mode == "OWNER_APPROVAL"
    assert OutboundMessage.objects.count() == 1
    assert ConversationHandling.objects.get().taken_over


def test_explicit_resend_of_failed_agent_send_is_human_authorized(ready):
    from inbox.services.drafts import resend_outbound

    _, run = run_initial(ready)
    original = OutboundMessage.objects.get(agent_run=run)
    original.status = "UNKNOWN"
    original.save()
    resend = resend_outbound(original, owner=ready.domain.owner)
    assert resend.authorization_mode == "DELEGATED_SCOPE"
    assert resend.agent_run_id is None
    assert ConversationHandling.objects.get().taken_over


def test_human_draft_edit_stops_agent(ready):
    from inbox.services.drafts import revise_draft

    _, run = run_initial(ready)
    revise_draft(
        draft=run.draft, owner=ready.domain.owner, subject="Re: Charge", body_text="A human reply."
    )
    assert ConversationHandling.objects.get().taken_over
    assert authorization_error(OutboundMessage.objects.get(agent_run=run))


def test_model_request_schema_and_refusal(owner, monkeypatch):
    answers = [
        {"name": "type", "type": "choice", "choice": "billing", "confidence": 0.9},
        {"name": "risk", "type": "choice", "choice": "low", "confidence": 0.95},
        {"name": "missing_information", "type": "predicate", "probability": 0.99},
        {"name": "automated", "type": "predicate", "probability": 0.01},
    ]
    request = Mock(return_value={"answers": answers})
    monkeypatch.setattr(handling_model, "request_model", request)
    result = handling_model.classify(owner, {"body": "Please refund an unrecognized charge"})
    assert result["type"] == "billing" and result["risk"] == "low"
    body = request.call_args.args[2]
    assert body["input"][0]["role"] == "user"
    assert body["input"][0]["content"][0]["type"] == "input_json"
    assert all("instructions" in q for q in body["questions"])
    answers[0]["confidence"] = float("nan")
    with pytest.raises(handling_model.ModelUnavailable):
        handling_model.classify(owner, {})


def test_action_adapter_rejects_external_tools(owner, monkeypatch):
    request = Mock(
        return_value={"output": [{"type": "function_call", "name": "refund", "arguments": "{}"}]}
    )
    monkeypatch.setattr(handling_model, "request_model", request)
    with pytest.raises(handling_model.ModelUnavailable):
        handling_model.propose(owner, {}, {}, phase="initial")
    assert request.call_args.args[2]["store"] is False
    assert [tool["name"] for tool in request.call_args.args[2]["tools"]] == ["handle_conversation"]


def test_disabled_policy_ignores_new_mail_but_explicit_handoff_works(ready):
    p = account_policy(ready.domain.owner)
    p.mode = "off"
    save_policy(p)
    on_inbound(ready)
    assert not HandlingRun.objects.exists()
    state, _ = run_initial(ready)
    assert state.status == "waiting"


def test_no_reminder_when_disabled(ready):
    p = account_policy(ready.domain.owner)
    p.followup_enabled = False
    save_policy(p)
    state, _ = run_initial(ready)
    assert state.status == "waiting" and state.followup_at is None
    assert schedule_handling(timezone.now() + timedelta(days=30)) == 0


def test_model_tag_cannot_change_rule_within_run(ready):
    p = account_policy(ready.domain.owner)
    p.rules = [
        {"name": "approved", "tags": ["approved"], "action": "auto"},
        {"name": "default", "action": "draft"},
    ]
    save_policy(p)
    handling_model.propose.return_value["add_tags"] = ["approved"]
    state, run = run_initial(ready)
    assert state.status == "review" and run.action == "draft"
    assert not OutboundMessage.objects.exists()


def test_takeover_ui_and_needs_review_filter(client, ready):
    client.force_login(ready.domain.owner)
    response = client.post(
        reverse("conversation_handling", args=[ready.conversation_id]), {"action": "takeover"}
    )
    assert response.status_code == 302
    assert ConversationHandling.objects.get().taken_over
    state = ConversationHandling.objects.get()
    state.status = "review"
    state.save()
    response = client.get(reverse("inbox"), {"needs_review": "1"})
    assert response.status_code == 200
    assert b"Needs review" in response.content


def test_save_mode_with_untouched_add_rule_form(client, ready):
    client.force_login(ready.domain.owner)
    response = client.post(
        reverse("agent_settings"),
        {
            "mode": "draft",
            "followup_enabled": "on",
            "followup_hours": "72",
            "rules-TOTAL_FORMS": "1",
            "rules-INITIAL_FORMS": "0",
            "rules-MIN_NUM_FORMS": "0",
            "rules-MAX_NUM_FORMS": "30",
            "rules-0-name": "",
            "rules-0-action": "auto",
            "rules-0-ORDER": "",
        },
    )
    assert response.status_code == 302
    assert account_policy(ready.domain.owner).mode == "draft"
    assert account_policy(ready.domain.owner).rules == []


def test_explicit_high_risk_archive_rule_never_authorizes_reply(ready):
    p = account_policy(ready.domain.owner)
    p.rules = [
        {
            "name": "Keep threats",
            "risks": ["high"],
            "action": "archive",
            "add_tags": ["suspected-phishing"],
        }
    ]
    save_policy(p)
    handling_model.classify.return_value.update(risk="high", type="spam")
    ready.spf_verdict = "FAIL"
    ready.save()
    state, run = run_initial(ready)
    ready.conversation.refresh_from_db()
    assert state.status == "done" and run.action == "archive"
    assert ready.conversation.archived_at is not None
    assert ready.conversation.tags.filter(normalized_name="suspected-phishing").exists()
    assert not OutboundMessage.objects.exists()
    handling_model.propose.assert_not_called()


def test_retention_during_model_call_cannot_recreate_expired_content(ready):
    def proposal(*args, **kwargs):
        Message.objects.filter(id=ready.id).update(
            normalized_purged_at=timezone.now(), text_body=""
        )
        return {
            "action": "reply",
            "body": "Old confidential content",
            "waiting_for_reply": False,
            "add_tags": [],
            "remove_tags": [],
        }

    handling_model.propose.side_effect = proposal
    state, run = run_initial(ready)
    assert run.status == "superseded"
    assert state.status == "review"
    assert not OutboundMessage.objects.exists()
    assert not ready.conversation.reply_drafts.exists()


def test_worker_rechecks_terminal_run_after_acquiring_lock(ready, monkeypatch):
    from inbox.services import handling

    state = set_handling(ready.conversation, owner=ready.domain.owner, action="handoff")
    run = HandlingRun.objects.get(conversation=ready.conversation, generation=state.generation)
    original = handling.get_outbound_control

    def completed_before_lock(*args, **kwargs):
        control = original(*args, **kwargs)
        HandlingRun.objects.filter(pk=run.pk).update(status="complete")
        return control

    monkeypatch.setattr(handling, "get_outbound_control", completed_before_lock)
    run_handling(str(run.id))
    handling_model.classify.assert_not_called()
    assert not OutboundMessage.objects.exists()


def test_database_prevents_two_outbounds_for_same_run(ready):
    from django.db import IntegrityError, transaction

    from inbox.services.drafts import _outbound_for_revision, create_authored_draft

    _, run = run_initial(ready)
    draft = create_authored_draft(
        message=ready, owner=ready.domain.owner, subject="Re: Charge", body_text="Duplicate"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        outbound = _outbound_for_revision(
            draft=draft, revision=draft.current_revision, authorization_mode="DELEGATED_SCOPE"
        )
        outbound.agent_run = run
        outbound.save()
    assert OutboundMessage.objects.filter(agent_run=run).count() == 1
