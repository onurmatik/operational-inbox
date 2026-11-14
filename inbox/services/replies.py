"""Send an owner's exact reply through the existing threaded delivery pipeline."""

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from inbox.models import (
    AuditEvent,
    Conversation,
    Domain,
    OutboundMessage,
    ReplyDraft,
    User,
)
from inbox.services.drafts import approve_exact_revision, create_authored_draft, revise_draft
from inbox.services.entitlements import can_manage_domain, for_user
from inbox.services.jobs import enqueue_job
from inbox.services.outbound import get_outbound_control
from inbox.services.reply_assistance import latest_inbound, validate_context


def _validate_text(subject, body_text):
    for name, value, limit in (("subject", subject, 998), ("body_text", body_text, 20000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValidationError({name: f"Enter between 1 and {limit:,} characters."})


def _persist_reply(
    *,
    conversation,
    owner,
    context_message_id,
    subject,
    body_text,
    draft_id=None,
    expected_revision_id=None,
    context_token=None,
):
    if context_token is not None:
        validate_context(conversation, context_token)
    message = latest_inbound(conversation)
    if message is None or str(message.id) != str(context_message_id):
        raise ValidationError("A newer message arrived. Refresh before replying.", code="stale")
    if message.is_quarantined or message.normalized_purged_at:
        raise ValidationError("The original message is unavailable for replying.")
    draft = None
    if draft_id:
        draft = (
            ReplyDraft.objects.select_for_update()
            .filter(
                id=draft_id,
                domain=conversation.domain,
                conversation=conversation,
                kind=ReplyDraft.Kind.REPLY,
            )
            .select_related("current_revision")
            .first()
        )
        if (
            draft is None
            or draft.current_revision is None
            or expected_revision_id is None
            or str(draft.current_revision_id) != str(expected_revision_id)
        ):
            raise ValidationError("The draft changed. Reload its current revision.", code="stale")
        if draft.current_revision.outbound_messages.exists():
            raise ValidationError(
                "This draft already has a send attempt. Refresh the conversation.", code="stale"
            )
    elif expected_revision_id:
        raise ValidationError("Select the saved draft before revising it.", code="stale")
    if draft is None or draft.is_stale or draft.context_message_id != message.id:
        # Preserve the old draft and its stale flag; bind a new draft to current inbound.
        return create_authored_draft(
            message=message,
            owner=owner,
            subject=subject,
            body_text=body_text,
            is_agent_generated=False,
        )
    assert draft.current_revision is not None
    if draft.current_revision.subject != subject or draft.current_revision.body_text != body_text:
        revision = revise_draft(
            draft=draft,
            owner=owner,
            subject=subject,
            body_text=body_text,
            expected_revision_id=expected_revision_id,
        )
        draft.current_revision = revision
    return draft


@transaction.atomic
def save_reply_draft(
    *,
    conversation,
    owner,
    context_message_id,
    subject,
    body_text,
    context_token,
    draft_id=None,
    expected_revision_id=None,
):
    if conversation.domain.owner_id != owner.id or not owner.is_active:
        raise ValidationError("Only the active domain owner can save a reply.")
    _validate_text(subject, body_text)
    get_outbound_control(owner, lock=True)
    domain = Domain.objects.select_for_update().get(id=conversation.domain_id, owner=owner)
    conversation = Conversation.objects.select_for_update().get(id=conversation.id, domain=domain)
    if domain.status == Domain.Status.DISABLED or not can_manage_domain(owner, domain):
        raise ValidationError("This conversation is read-only.")
    draft = _persist_reply(
        conversation=conversation,
        owner=owner,
        context_message_id=context_message_id,
        subject=subject,
        body_text=body_text,
        draft_id=draft_id,
        expected_revision_id=expected_revision_id,
        context_token=context_token,
    )
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=str(owner.id),
        event_type="draft.saved",
        object_type="ReplyDraft",
        object_id=draft.id,
        metadata={"revision_id": str(draft.current_revision_id)},
    )
    return draft


@transaction.atomic
def send_manual_reply(
    *,
    conversation: Conversation,
    owner: User,
    context_message_id: uuid.UUID,
    subject: str,
    body_text: str,
    idempotency_key: uuid.UUID,
    draft_id: uuid.UUID | None = None,
    expected_revision_id: uuid.UUID | None = None,
    context_token: str | None = None,
) -> OutboundMessage:
    if conversation.domain.owner_id != owner.id or not owner.is_active:
        raise ValidationError("Only the active domain owner can send a reply.")
    try:
        idempotency_key = uuid.UUID(str(idempotency_key))
        context_message_id = uuid.UUID(str(context_message_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Refresh the conversation and try again.") from exc
    _validate_text(subject, body_text)

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
    if domain.status == Domain.Status.DISABLED or not can_manage_domain(owner, domain):
        raise ValidationError("This conversation is read-only.")
    if not for_user(owner).outbound or domain.status == Domain.Status.DISABLED:
        raise ValidationError("Sending is unavailable for this domain.")
    if not domain.ownership_verified or not domain.outbound_ready:
        raise ValidationError("Outbound sending is not ready for this domain.")
    draft = _persist_reply(
        conversation=conversation,
        owner=owner,
        context_message_id=context_message_id,
        subject=subject,
        body_text=body_text,
        draft_id=draft_id,
        expected_revision_id=expected_revision_id,
        context_token=context_token,
    )
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
