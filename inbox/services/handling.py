"""Durable, generation-fenced email handling with narrowly scoped actions."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Conversation,
    ConversationHandling,
    HandlingRun,
    Message,
    OutboundMessage,
)
from inbox.services import handling_model as model
from inbox.services.conversations import apply_conversation_action
from inbox.services.drafts import _outbound_for_revision, create_authored_draft
from inbox.services.entitlements import can_manage_domain
from inbox.services.handling_policy import (
    effective_policy,
    no_reply,
    resolve_action,
    security_block,
)
from inbox.services.outbound import get_outbound_control
from inbox.services.tags import add_conversation_tag, remove_conversation_tag


def _audit(run: HandlingRun, event: str) -> None:
    AuditEvent.objects.create(
        domain=run.domain,
        actor_type=AuditEvent.ActorType.AGENT,
        event_type=event,
        object_type="Conversation",
        object_id=run.conversation_id,
        request_id=f"handling:{run.id}",
        metadata={
            "run_id": str(run.id),
            "generation": run.generation,
            "phase": run.phase,
            "action": run.action,
            "status": run.status,
            "policy_fingerprint": run.policy_fingerprint,
            "model": run.model,
            "prompt_version": run.prompt_version,
        },
    )


def _enqueue(state: ConversationHandling, phase="initial") -> HandlingRun:
    from inbox.services.jobs import enqueue_job

    run, _ = HandlingRun.objects.get_or_create(
        conversation=state.conversation,
        generation=state.generation,
        phase=phase,
        defaults={"domain": state.domain, "context_message": state.context_message},
    )
    enqueue_job(
        kind="handle_conversation",
        idempotency_key=f"handling:{run.id}",
        payload={"run_id": str(run.id)},
        domain=state.domain,
    )
    return run


@transaction.atomic
def on_inbound(message: Message) -> None:
    """Called only for newly persisted inbound mail, never historical scans."""
    if message.direction != Message.Direction.INBOUND or message.domain_tests.exists():
        return
    policy = effective_policy(message.domain)
    state = (
        ConversationHandling.objects.select_for_update()
        .filter(conversation=message.conversation)
        .first()
    )
    if state and state.context_message_id == message.id:
        return
    if state:
        state.generation = max(state.generation + 1, _next_generation(state.conversation))
        state.context_message = message
        state.followup_at = None
        state.classification = {}
        state.reason = ""
        state.status = "manual" if state.taken_over else "pending"
        state.save()
    elif settings.HANDLING_ENABLED and policy["mode"] != "off":
        state = ConversationHandling.objects.create(
            domain=message.domain,
            conversation=message.conversation,
            context_message=message,
            generation=_next_generation(message.conversation),
        )
    if state and not state.taken_over:
        if not settings.HANDLING_ENABLED or policy["mode"] == "off":
            state.status = "review"
            state.reason = "Automation is off."
            state.save(update_fields=("status", "reason", "updated_at"))
        else:
            _enqueue(state)


@transaction.atomic
def set_handling(conversation: Conversation, *, owner, action: str) -> ConversationHandling:
    if conversation.domain.owner_id != owner.id or not can_manage_domain(
        owner, conversation.domain
    ):
        raise ValidationError("This conversation is not writable.")
    if action not in {"handoff", "takeover"}:
        raise ValidationError("Choose handoff or takeover.")
    get_outbound_control(owner, lock=True)
    conversation = Conversation.objects.select_for_update().get(id=conversation.id)
    message = (
        conversation.messages.filter(direction=Message.Direction.INBOUND)
        .order_by("-received_at", "-created_at", "-id")
        .first()
    )
    if message is None:
        raise ValidationError("There is no inbound message to handle.")
    state, created = ConversationHandling.objects.get_or_create(
        conversation=conversation,
        defaults={
            "domain": conversation.domain,
            "context_message": message,
            "generation": _next_generation(conversation),
        },
    )
    if not created:
        state.generation = max(state.generation + 1, _next_generation(state.conversation))
    state.context_message = message
    state.taken_over = action == "takeover"
    state.explicit_handoff = action == "handoff"
    state.status = "manual" if state.taken_over else "pending"
    state.reason = ""
    state.followup_at = None
    state.save()
    AuditEvent.objects.create(
        domain=conversation.domain,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type=f"conversation.agent_{action}",
        object_type="Conversation",
        object_id=conversation.id,
        request_id=f"handling-control:{uuid.uuid4()}",
        metadata={"generation": state.generation},
    )
    if action == "handoff":
        _enqueue(state)
    return state


def _latest_outbound(conversation_id, *, exclude=None):
    return (
        OutboundMessage.objects.filter(conversation_id=conversation_id)
        .exclude(id=exclude)
        .order_by("-created_at", "-id")
        .values_list("id", flat=True)
        .first()
    )


def _current(
    state: ConversationHandling,
    run: HandlingRun,
    policy: dict,
    *,
    token=None,
    exclude_outbound=None,
) -> bool:
    run.context_message.refresh_from_db()
    run.domain.refresh_from_db()
    if run.context_message.normalized_purged_at or run.context_message.is_quarantined:
        return False
    latest = (
        Message.objects.filter(
            conversation_id=run.conversation_id, direction=Message.Direction.INBOUND
        )
        .order_by("-received_at", "-created_at", "-id")
        .values_list("id", flat=True)
        .first()
    )
    return bool(
        settings.HANDLING_ENABLED
        and not state.taken_over
        and state.generation == run.generation
        and state.context_message_id == run.context_message_id
        and latest == run.context_message_id
        and (token is None or state.lease_token == token)
        and policy["fingerprint"] == run.policy_fingerprint
        and _latest_outbound(run.conversation_id, exclude=exclude_outbound)
        == run.baseline_outbound_id
        and can_manage_domain(run.domain.owner, run.domain)
        and run.domain.owner.is_active
    )


def authorization_error(outbound: OutboundMessage) -> str:
    run = outbound.agent_run
    if run is None or run.draft_id != outbound.revision.draft_id or run.action != "reply":
        return "Agent authorization is missing."
    revision = outbound.revision
    context = run.context_message
    if (
        outbound.content_hash != revision.content_hash
        or outbound.subject != revision.subject
        or outbound.body_text != revision.body_text
        or outbound.to_address != (context.reply_to_address or context.from_address)
    ):
        return "Agent reply no longer matches its authorized snapshot."
    state = ConversationHandling.objects.filter(conversation_id=outbound.conversation_id).first()
    if (
        state is None
        or run.domain_id != outbound.domain_id
        or run.conversation_id != outbound.conversation_id
    ):
        return "Agent authorization does not cover this conversation."
    if not _current(state, run, effective_policy(run.domain), exclude_outbound=outbound.id):
        return "The agent context or policy changed."
    return security_block(run.context_message) or (
        "Automated mail must not receive replies." if no_reply(run.context_message) else ""
    )


def _evidence(run: HandlingRun, policy: dict) -> dict:
    messages = list(
        run.conversation.messages.filter(normalized_purged_at__isnull=True).order_by(
            "received_at", "created_at", "id"
        )
    )
    if len(messages) > 30 or any(m.is_quarantined for m in messages):
        raise model.ModelUnavailable("Conversation context needs review.")
    evidence = {
        "domain_knowledge": policy["knowledge"],
        "messages": [
            {
                "id": str(m.id),
                "direction": m.direction,
                "sender": m.from_address,
                "subject": m.subject,
                "body": m.text_body,
            }
            for m in messages
        ],
        "newest_message_id": str(run.context_message_id),
        "security": {
            key: getattr(run.context_message, key)
            for key in (
                "spam_verdict",
                "virus_verdict",
                "spf_verdict",
                "dkim_verdict",
                "dmarc_verdict",
            )
        },
        "headers": run.context_message.automation_headers,
        "tags": list(run.conversation.tags.values_list("name", flat=True)),
    }
    if len(json.dumps(evidence).encode()) > 90000:
        raise model.ModelUnavailable("Conversation context exceeds the agent limit.")
    return evidence


def _finish(state, run, status, reason, *, action="", classification=None):
    run.status = "complete" if status in {"done", "waiting"} else status
    run.reason = reason[:160]
    run.action = action or run.action
    run.finished_at = timezone.now()
    if classification is not None:
        run.classification = classification
        state.classification = classification
    run.save()
    state.status, state.reason = status, reason[:160]
    state.lease_until = None
    state.lease_token = None
    if status != "waiting":
        state.followup_at = None
    state.save()
    _audit(run, "agent.action_completed" if run.status == "complete" else "agent.needs_review")


def run_handling(run_id: str) -> None:
    run = (
        HandlingRun.objects.select_related("domain__owner", "conversation", "context_message")
        .filter(id=run_id)
        .first()
    )
    if run is None or run.status in {"complete", "review", "superseded"}:
        return
    token = uuid.uuid4()
    with transaction.atomic():
        get_outbound_control(run.domain.owner, lock=True)
        state = ConversationHandling.objects.select_for_update().get(conversation=run.conversation)
        run.refresh_from_db()
        if run.status in {"complete", "review", "superseded"}:
            return
        if state.taken_over or state.generation != run.generation:
            run.status = "superseded"
            run.save(update_fields=("status", "updated_at"))
            return
        if state.lease_until and state.lease_until > timezone.now():
            raise model.ModelRetryable("Conversation is already being handled.")
        policy = effective_policy(run.domain)
        run.policy_fingerprint = policy["fingerprint"]
        run.baseline_outbound_id = _latest_outbound(run.conversation_id)
        run.status = "running"
        run.model = settings.HANDLING_RESPONSE_MODEL
        run.save()
        state.lease_token = token
        state.lease_until = timezone.now() + timedelta(minutes=4)
        state.status = "running"
        state.save()
        block = security_block(run.context_message)
        if (
            run.context_message.is_quarantined
            or run.context_message.virus_verdict != Message.Verdict.PASS
            or run.context_message.normalized_purged_at
            or not can_manage_domain(run.domain.owner, run.domain)
            or not settings.HANDLING_ENABLED
        ):
            _finish(state, run, "review", block or "Agent handling is unavailable.")
            return
        # A human/external agent send after this inbound must not get a second reply.
        latest_send = run.conversation.outbound_messages.order_by("-created_at").first()
        if (
            run.phase == "initial"
            and latest_send
            and latest_send.created_at >= run.context_message.created_at
        ):
            _finish(state, run, "review", "A reply already exists; review before handling.")
            return
        if run.phase == "escalate":
            _finish(state, run, "review", "No reply after the reminder.")
            return
    try:
        evidence = _evidence(run, policy)
        classification = model.classify(run.domain.owner, evidence)
        action, tags, reason = resolve_action(
            policy, classification, evidence["tags"], explicit=state.explicit_handoff
        )
        if block and action != "archive":
            action, reason = "review", block
        plan: dict[str, Any] = {
            "action": "review",
            "body": "",
            "add_tags": [],
            "remove_tags": [],
            "waiting_for_reply": False,
        }
        if action != "review" and (
            no_reply(run.context_message) or classification["automated"] >= 0.5
        ):
            action = (
                "archive"
                if classification["type"] in {"newsletter", "spam"} and action == "archive"
                else "done"
            )
            reason = "Automated message; no reply."
        if action in {"auto", "draft"}:
            plan = model.propose(run.domain.owner, evidence, classification, phase=run.phase)
            if plan["action"] == "reply" and not model.safe_reply(
                run.domain.owner, evidence, plan["body"]
            ):
                plan["action"] = "review"
                reason = "Proposed reply requires review."
        with transaction.atomic():
            get_outbound_control(run.domain.owner, lock=True)
            state = ConversationHandling.objects.select_for_update().get(
                conversation=run.conversation
            )
            if not _current(state, run, effective_policy(run.domain), token=token):
                run.status = "superseded"
                run.save(update_fields=("status", "updated_at"))
                if state.lease_token == token:
                    state.lease_token = None
                    state.lease_until = None
                    if state.generation == run.generation:
                        state.status = "review"
                        state.reason = "Conversation changed during handling."
                    state.save()
                return
            classification_tag = "agent:" + classification["type"]
            for tag in [classification_tag, *tags, *plan["add_tags"]]:
                add_conversation_tag(run.conversation, tag, agent_action=True)
            for tag in plan["remove_tags"]:
                remove_conversation_tag(run.conversation, tag, agent_action=True)
            run.classification = classification
            if action == "archive" or (action == "auto" and plan["action"] == "archive"):
                apply_conversation_action(run.conversation, "archive", agent_action=True)
                _finish(state, run, "done", reason, action="archive", classification=classification)
            elif action == "done" or (action == "auto" and plan["action"] == "done"):
                _finish(state, run, "done", reason, action="done", classification=classification)
            elif action in {"auto", "draft"} and plan["action"] == "reply":
                draft = create_authored_draft(
                    message=run.context_message,
                    owner=run.domain.owner,
                    subject="Re: " + run.context_message.subject.removeprefix("Re: ")[:994],
                    body_text=plan["body"],
                )
                run.draft = draft
                run.action = "reply" if action == "auto" else "draft"
                run.save()
                if action == "auto":
                    # Keep the draft when pause/quota/domain readiness prevents sending.
                    try:
                        with transaction.atomic():
                            assert draft.current_revision is not None
                            outbound = _outbound_for_revision(
                                draft=draft,
                                revision=draft.current_revision,
                                authorization_mode=OutboundMessage.AuthorizationMode.AGENT_POLICY,
                            )
                            outbound.agent_run = run
                            outbound.save(update_fields=("agent_run", "updated_at"))
                    except ValidationError:
                        _finish(
                            state,
                            run,
                            "review",
                            "Draft saved; sending is unavailable.",
                            classification=classification,
                        )
                        return
                    waiting = plan["waiting_for_reply"] or run.phase == "followup"
                    if waiting and policy["followup_enabled"]:
                        state.followup_at = timezone.now() + timedelta(
                            hours=policy["followup_hours"]
                        )
                    _finish(
                        state,
                        run,
                        "waiting" if waiting else "done",
                        "Reply queued.",
                        classification=classification,
                    )
                else:
                    _finish(
                        state, run, "review", "Reply draft is ready.", classification=classification
                    )
            else:
                _finish(
                    state,
                    run,
                    "review",
                    reason if action == "review" else "Agent requested review.",
                    action="review",
                    classification=classification,
                )
    except model.ModelRetryable:
        with transaction.atomic():
            state = ConversationHandling.objects.select_for_update().get(
                conversation=run.conversation
            )
            if state.lease_token == token:
                state.lease_until = None
                state.lease_token = None
                state.save()
        raise
    except (model.ModelUnavailable, ValidationError) as exc:
        with transaction.atomic():
            state = ConversationHandling.objects.select_for_update().get(
                conversation=run.conversation
            )
            if state.generation == run.generation and state.lease_token == token:
                reason = (
                    str(exc)
                    if isinstance(exc, model.ModelUnavailable)
                    else "Agent action requires review."
                )
                _finish(state, run, "review", reason)


def fail_handling(run_id: str) -> None:
    run = HandlingRun.objects.filter(id=run_id).first()
    if run is None:
        return
    with transaction.atomic():
        state = ConversationHandling.objects.select_for_update().get(
            conversation_id=run.conversation_id
        )
        if (
            state.generation == run.generation
            and not state.taken_over
            and run.status not in {"complete", "review", "superseded"}
        ):
            _finish(state, run, "review", "Agent could not finish. Hand to agent to retry.")


def schedule_handling(now=None) -> int:
    now = now or timezone.now()
    count = 0
    for state in ConversationHandling.objects.filter(
        status="waiting", taken_over=False, followup_at__lte=now
    ).select_related("domain__owner", "conversation", "context_message"):
        with transaction.atomic():
            get_outbound_control(state.domain.owner, lock=True)
            state = ConversationHandling.objects.select_for_update().get(pk=state.pk)
            if state.status != "waiting" or state.taken_over:
                continue
            run = (
                HandlingRun.objects.filter(
                    conversation=state.conversation, generation=state.generation
                )
                .order_by("-created_at")
                .first()
            )
            outbound = run.outbound.first() if run else None
            if not run or not outbound:
                state.status, state.reason = "review", "Follow-up context is unavailable."
            elif outbound.status in {"QUEUED", "SUBMITTING"}:
                continue
            elif outbound.status not in {"ACCEPTED", "DELIVERED"}:
                state.status, state.reason = "review", "Delivery needs review before follow-up."
            elif not _current(
                state, run, effective_policy(state.domain), exclude_outbound=outbound.id
            ):
                state.status, state.reason = "review", "Context changed; follow-up stopped."
            else:
                due = (outbound.accepted_at or outbound.created_at) + timedelta(
                    hours=effective_policy(state.domain)["followup_hours"]
                )
                if due > now:
                    state.followup_at = due
                    state.save(update_fields=("followup_at", "updated_at"))
                    continue
                _enqueue(state, "followup" if run.phase == "initial" else "escalate")
                state.status = "pending"
                count += 1
            state.followup_at = None
            state.save()
    return count


def handling_dict(conversation: Conversation, *, details=False) -> dict | None:
    state = getattr(conversation, "handling", None)
    if state is None or state.domain_id != conversation.domain_id:
        return None
    result = {
        "status": state.status,
        "label": state.get_status_display(),
        "taken_over": state.taken_over,
        "classification": state.classification,
        "reason": state.reason,
        "followup_at": state.followup_at,
    }
    if details:
        result["history"] = list(
            conversation.handling_runs.order_by("-created_at").values(
                "id", "created_at", "phase", "action", "status", "reason"
            )[:10]
        )
    return result


def interrupt_handling(conversation: Conversation, *, takeover=True) -> None:
    """Fence pending work when a person or external agent changes the conversation."""
    from django.db.models import F

    states = ConversationHandling.objects.filter(conversation=conversation, taken_over=False)
    states.update(
        generation=F("generation") + 1,
        taken_over=takeover,
        status="manual" if takeover else "review",
        followup_at=None,
        reason="Conversation changed outside the built-in agent.",
        updated_at=timezone.now(),
    )


def _next_generation(conversation: Conversation) -> int:
    from django.db.models import Max

    return (
        HandlingRun.objects.filter(conversation=conversation).aggregate(value=Max("generation"))[
            "value"
        ]
        or 0
    ) + 1
