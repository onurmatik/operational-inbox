"""Owner-requested writing assistance. No conversation actions or sending authority."""

import hashlib
import json

from django.core import signing
from django.core.exceptions import ValidationError

from inbox.models import Domain, Message
from inbox.services import reply_assist_model as model
from inbox.services.drafts import current_reply_draft, draft_reference
from inbox.services.entitlements import can_manage_domain

SALT = "reply-assistance-v1"


def latest_inbound(conversation):
    return (
        conversation.messages.filter(
            domain=conversation.domain, direction=Message.Direction.INBOUND
        )
        .order_by("-received_at", "-created_at", "-id")
        .first()
    )


def snapshot(conversation):
    data = {
        "conversation": str(conversation.id),
        "owner": str(conversation.domain.owner_id),
        "domain": str(conversation.domain_id),
        "messages": list(
            conversation.messages.filter(domain=conversation.domain)
            .order_by("id")
            .values_list("id", "updated_at", "normalized_purged_at")
        ),
        "outbound": list(
            conversation.outbound_messages.filter(domain=conversation.domain)
            .order_by("id")
            .values_list("id", "updated_at", "status", "content_hash")
        ),
        "draft": draft_reference(current_reply_draft(conversation)),
    }
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


def context_token(conversation):
    return signing.dumps(snapshot(conversation), salt=SALT)


def validate_context(conversation, token):
    try:
        digest = signing.loads(token or "", salt=SALT, max_age=3600)
    except (signing.BadSignature, TypeError) as exc:
        raise ValidationError(
            "Reply context expired. Refresh the conversation.", code="stale"
        ) from exc
    if digest != snapshot(conversation):
        raise ValidationError(
            "The conversation or draft changed. Refresh before continuing.", code="stale"
        )


def require_reply_access(conversation):
    domain = Domain.objects.select_related("owner").get(id=conversation.domain_id)
    conversation.domain = domain
    if (
        domain.status == Domain.Status.DISABLED
        or not domain.owner.is_active
        or not can_manage_domain(domain.owner, domain)
    ):
        raise ValidationError("This conversation is read-only.", code="read_only")
    message = latest_inbound(conversation)
    if message is None or message.is_quarantined or message.normalized_purged_at:
        raise ValidationError("The original message is unavailable for replying.")
    return message


def evidence(conversation):
    newest = require_reply_access(conversation)
    messages = list(
        conversation.messages.filter(domain=conversation.domain).order_by(
            "received_at", "created_at", "id"
        )
    )
    # Include visible attempts with delivery state; a queued/unknown reply is not proof of sending.
    outbound = list(
        conversation.outbound_messages.filter(
            domain=conversation.domain,
        ).order_by("created_at", "id")
    )
    represented = {message.rfc_message_id for message in messages}
    entries = [
        (
            m.received_at,
            {
                "direction": m.direction,
                "sender": m.from_address,
                "subject": m.subject,
                "body": m.text_body,
            },
        )
        for m in messages
        if not m.is_quarantined and not m.normalized_purged_at
    ]
    entries.extend(
        (
            o.created_at,
            {
                "direction": "OUTBOUND",
                "sender": o.from_address,
                "subject": o.subject,
                "body": o.body_text,
                "delivery_status": o.status,
            },
        )
        for o in outbound
        if o.rfc_message_id not in represented
    )
    if len(messages) + sum(o.rfc_message_id not in represented for o in outbound) > 30:
        raise model.ModelUnavailable("This conversation exceeds the AI context limit.")
    result = {
        "messages": [entry for _, entry in sorted(entries, key=lambda item: item[0])],
        "newest_message_id": str(newest.id),
        "unavailable_messages": sum(
            bool(m.is_quarantined or m.normalized_purged_at) for m in messages
        ),
    }
    return result


def assist(conversation, *, operation, token, subject="", body_text="", instructions=""):
    require_reply_access(conversation)
    validate_context(conversation, token)
    if operation == "validate":
        return {"context_token": token}
    if operation not in {"goals", "generate", "review"}:
        raise ValidationError("Choose a supported writing action.")
    for value, limit in ((subject, 998), (body_text, 20000), (instructions, 2000)):
        if not isinstance(value, str) or len(value) > limit:
            raise ValidationError("The writing request exceeds its character limit.")
    if operation == "review" and not body_text.strip():
        raise ValidationError("Write a reply before requesting a review.")
    if operation == "generate" and not instructions.strip():
        raise ValidationError("Choose a reply option or add instructions.")
    context = evidence(conversation)
    context.update(subject=subject, body_text=body_text, owner_instructions=instructions)
    if len(json.dumps(context).encode()) > 90000:
        raise model.ModelUnavailable("This conversation exceeds the AI context limit.")
    result = model.suggest(conversation.domain.owner, operation, context)
    require_reply_access(conversation)
    validate_context(conversation, token)
    return {**result, "context_token": token}
