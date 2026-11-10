"""On-demand reply goals, bound to the conversation the owner actually reviewed."""

import hashlib
import json

from django.core import signing
from django.core.exceptions import ValidationError

from inbox.models import ConversationHandling, HandlingRun, Message
from inbox.services import handling_model as model
from inbox.services.entitlements import can_manage_domain
from inbox.services.handling import _evidence, _latest_outbound
from inbox.services.handling_policy import content_block, effective_policy, no_reply

SALT = "conversation-reply-brief-v1"


def snapshot(conversation):
    policy = effective_policy(conversation.domain)
    messages = list(
        conversation.messages.order_by("received_at", "created_at", "id").values_list(
            "id", "updated_at", "normalized_purged_at"
        )
    )
    state = (
        ConversationHandling.objects.filter(conversation=conversation)
        .values_list("generation", "status", "taken_over")
        .first()
    )
    data = {
        "conversation": str(conversation.id),
        "owner": str(conversation.domain.owner_id),
        "domain": str(conversation.domain_id),
        "messages": messages,
        "tags": sorted(conversation.tags.values_list("name", flat=True)),
        "policy": policy["fingerprint"],
        "state": state,
        "outbound": _latest_outbound(conversation.id),
    }
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


def validate_context(conversation, token):
    try:
        digest = signing.loads(token or "", salt=SALT, max_age=3600)
    except (signing.BadSignature, TypeError) as exc:
        raise ValidationError(
            "Reply options expired. Refresh this conversation.", code="stale"
        ) from exc
    if digest != snapshot(conversation):
        raise ValidationError(
            "Conversation or settings changed. Refresh this conversation.", code="stale"
        )


def validate_brief(brief):
    if not isinstance(brief, dict) or set(brief) != {"instructions", "delivery"}:
        raise ValidationError("Choose reply instructions and a delivery option.")
    instructions = brief["instructions"]
    if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 2000:
        raise ValidationError("Add reply instructions of up to 2,000 characters.")
    if not isinstance(brief["delivery"], str) or brief["delivery"] not in {"draft", "auto"}:
        raise ValidationError("Choose a supported delivery option.")
    return {"instructions": instructions.strip(), "delivery": brief["delivery"]}


def reply_composer(conversation):
    if not can_manage_domain(conversation.domain.owner, conversation.domain):
        return None
    message = (
        conversation.messages.filter(direction=Message.Direction.INBOUND)
        .order_by("-received_at", "-created_at", "-id")
        .first()
    )
    if message is None:
        return None
    policy = effective_policy(conversation.domain)
    return {
        "token": signing.dumps(snapshot(conversation), salt=SALT),
        "automatic_allowed": policy["mode"] in {"auto", "off"},
        "followup_enabled": policy["followup_enabled"],
    }


def suggest_goals(conversation, token):
    validate_context(conversation, token)
    message = (
        conversation.messages.filter(direction=Message.Direction.INBOUND)
        .order_by("-received_at", "-created_at", "-id")
        .first()
    )
    if message is None:
        raise ValidationError("There is no inbound message to reply to.")
    # Reply goals do not authorize sending or choose a recipient.
    block = content_block(message)
    if block:
        raise ValidationError(block)
    if no_reply(message):
        raise ValidationError("Automated messages do not support reply suggestions.")
    evidence = _evidence(
        HandlingRun(conversation=conversation, context_message=message, domain=conversation.domain),
        effective_policy(conversation.domain),
    )
    goals = model.suggest_reply_goals(conversation.domain.owner, evidence)
    # Never return suggestions for a thread changed while the model was running.
    validate_context(conversation, token)
    return goals
