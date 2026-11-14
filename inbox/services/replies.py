"""Send an owner's exact reply through the existing threaded delivery pipeline."""

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from inbox.models import (
    AuditEvent,
    Conversation,
    Domain,
    Message,
    OutboundMessage,
    ReplyDraft,
    User,
)
from inbox.services.drafts import approve_exact_revision, create_authored_draft
from inbox.services.entitlements import can_manage_domain, for_user
from inbox.services.handling import set_handling
from inbox.services.jobs import enqueue_job
from inbox.services.outbound import get_outbound_control


@transaction.atomic
def send_manual_reply(
    *,
    conversation: Conversation,
    owner: User,
    context_message_id: uuid.UUID,
    subject: str,
    body_text: str,
    idempotency_key: uuid.UUID,
) -> OutboundMessage:
    if conversation.domain.owner_id != owner.id or not owner.is_active:
        raise ValidationError("Only the active domain owner can send a reply.")
    try:
        idempotency_key = uuid.UUID(str(idempotency_key))
        context_message_id = uuid.UUID(str(context_message_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Refresh the conversation and try again.") from exc
    for name, value, limit in (("subject", subject, 998), ("body_text", body_text, 20000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValidationError({name: f"Enter between 1 and {limit:,} characters."})

    # Serialize key lookup and quota checks with every other send for this owner.
    get_outbound_control(owner, lock=True)
    domain = Domain.objects.select_for_update().get(id=conversation.domain_id, owner=owner)
    conversation = Conversation.objects.select_for_update().get(id=conversation.id, domain=domain)
    existing = (
        OutboundMessage.objects.filter(domain=domain, idempotency_key=idempotency_key)
        .select_related("revision__draft")
        .first()
    )
    if existing is not None:
        if (
            existing.conversation_id != conversation.id
            or existing.revision.draft.kind != ReplyDraft.Kind.REPLY
            or existing.revision.draft.context_message_id != context_message_id
            or existing.subject != subject
            or existing.body_text != body_text
        ):
            raise ValidationError(
                "This send request was already used for different content. "
                "Refresh the conversation.",
                code="idempotency_conflict",
            )
        # A lost response must never create another attempt, even for FAILED or UNKNOWN.
        return existing
    if not can_manage_domain(owner, domain):
        raise ValidationError("This conversation is read-only.")
    if not for_user(owner).outbound or domain.status == Domain.Status.DISABLED:
        raise ValidationError("Sending is unavailable for this domain.")
    if not domain.ownership_verified or not domain.outbound_ready:
        raise ValidationError("Outbound sending is not ready for this domain.")
    message = (
        conversation.messages.filter(domain=domain, direction=Message.Direction.INBOUND)
        .order_by("-received_at", "-created_at", "-id")
        .first()
    )
    if message is None or message.id != context_message_id:
        raise ValidationError("A newer message arrived. Refresh the conversation before replying.")
    if message.normalized_purged_at:
        raise ValidationError("The original message is no longer available.")
    draft = create_authored_draft(
        message=message,
        owner=owner,
        subject=subject,
        body_text=body_text,
        is_agent_generated=False,
    )
    # Persist takeover even when this conversation has never been handled by the agent.
    set_handling(conversation, owner=owner, action="takeover")
    revision = draft.current_revision
    assert revision is not None
    outbound = approve_exact_revision(
        draft=draft,
        revision_id=revision.id,
        content_hash=revision.content_hash,
        owner=owner,
    )
    outbound.idempotency_key = idempotency_key
    outbound.save(update_fields=("idempotency_key", "updated_at"))
    enqueue_job(
        kind="send_outbound",
        idempotency_key=f"outbound:{outbound.id}",
        payload={"outbound_id": str(outbound.id)},
        domain=domain,
    )
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=str(owner.id),
        event_type="outbound.replied",
        object_type="OutboundMessage",
        object_id=outbound.id,
        request_id=f"reply:{idempotency_key}",
    )
    return outbound
