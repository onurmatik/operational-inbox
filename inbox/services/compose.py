from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Conversation,
    Domain,
    DraftApproval,
    OutboundMessage,
    ReplyDraft,
    ReplyDraftRevision,
    User,
    compose_content_digest,
    reject_header_injection,
)
from inbox.services.entitlements import can_manage_domain, for_user
from inbox.services.jobs import enqueue_job
from inbox.services.outbound import get_outbound_control, require_outbound_capacity
from inbox.services.threading import normalize_subject


def validate_composed_email(
    *, from_address: str, to_address: str, subject: str, body_text: str
) -> None:
    errors: dict[str, list[str]] = {}
    for field, value in (("from_address", from_address), ("to_address", to_address)):
        try:
            reject_header_injection(value, field)
            validate_email(value)
            if len(value) > 320:
                raise ValidationError("Use an email address of at most 320 characters.")
        except ValidationError as exc:
            errors[field] = exc.messages
    for field, value, limit in (("subject", subject, 998), ("body_text", body_text, 20000)):
        if not value.strip():
            errors[field] = ["This field is required."]
        elif len(value) > limit:
            errors[field] = [f"Use at most {limit} characters."]
    try:
        reject_header_injection(subject, "subject")
    except ValidationError as exc:
        errors["subject"] = exc.messages
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def send_composed_email(
    *,
    domain: Domain,
    owner: User,
    from_address: str,
    to_address: str,
    subject: str,
    body_text: str,
    idempotency_key: uuid.UUID,
    authorization_mode: str = OutboundMessage.AuthorizationMode.DELEGATED_SCOPE,
) -> OutboundMessage:
    """Atomically snapshot and queue one new email, without an unsent draft workflow."""
    validate_composed_email(
        from_address=from_address, to_address=to_address, subject=subject, body_text=body_text
    )
    if domain.owner_id != owner.id or not owner.is_active:
        raise ValidationError("Only the active domain owner can send email.", code="send_forbidden")
    if authorization_mode not in OutboundMessage.AuthorizationMode.values:
        raise ValidationError("Select a supported send authorization.")
    try:
        idempotency_key = uuid.UUID(str(idempotency_key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({"idempotency_key": "Use a valid UUID."}) from exc

    # Use the same account lock as reply sending, including the initial key lookup.
    get_outbound_control(owner, lock=True)
    domain = Domain.objects.select_for_update().get(id=domain.id, owner=owner)
    if from_address.rsplit("@", 1)[1].casefold() != domain.hostname.casefold():
        raise ValidationError({"from_address": "The From address must belong to this domain."})
    digest = compose_content_digest(from_address, to_address, subject, body_text)
    existing = OutboundMessage.objects.filter(
        domain=domain, idempotency_key=idempotency_key
    ).first()
    if existing is not None:
        if existing.content_hash != digest:
            raise ValidationError(
                "This idempotency key was already used for different email content.",
                code="idempotency_conflict",
            )
        return existing
    if not can_manage_domain(owner, domain):
        raise ValidationError("This domain is read-only.", code="domain_read_only")
    if not for_user(owner).outbound or domain.status == Domain.Status.DISABLED:
        raise ValidationError("Sending is unavailable for this domain.", code="send_unavailable")
    if not domain.ownership_verified or not domain.outbound_ready:
        raise ValidationError(
            "Outbound sending is not ready for this domain.", code="outbound_not_ready"
        )
    require_outbound_capacity(domain)

    now = timezone.now()
    conversation = Conversation.objects.create(
        domain=domain,
        subject=subject,
        normalized_subject=normalize_subject(subject),
        first_message_at=now,
        last_message_at=now,
    )
    snapshot = ReplyDraft.objects.create(
        domain=domain, conversation=conversation, kind=ReplyDraft.Kind.COMPOSE
    )
    revision = ReplyDraftRevision(
        domain=domain,
        draft=snapshot,
        number=1,
        from_address=from_address,
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        author=owner,
        is_agent_generated=authorization_mode == OutboundMessage.AuthorizationMode.DELEGATED_SCOPE,
    )
    revision.full_clean()
    revision.save()
    snapshot.current_revision = revision
    snapshot.save(update_fields=("current_revision", "updated_at"))
    if authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL:
        approval = DraftApproval(
            domain=domain, revision=revision, approved_by=owner, content_hash=revision.content_hash
        )
        approval.full_clean()
        approval.save()
    outbound = OutboundMessage(
        domain=domain,
        conversation=conversation,
        revision=revision,
        idempotency_key=idempotency_key,
        authorization_mode=authorization_mode,
        from_address=from_address,
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        content_hash=revision.content_hash,
        rfc_message_id=f"<{uuid.uuid4()}@operationalinbox.com>",
    )
    outbound.full_clean()
    outbound.save()
    enqueue_job(
        kind="send_outbound",
        idempotency_key=f"outbound:{outbound.id}",
        payload={"outbound_id": str(outbound.id)},
        domain=domain,
    )
    AuditEvent.objects.create(
        domain=domain,
        actor_type=(
            AuditEvent.ActorType.OWNER
            if authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL
            else AuditEvent.ActorType.AGENT
        ),
        actor_id=str(owner.id),
        event_type="outbound.composed",
        object_type="OutboundMessage",
        object_id=outbound.id,
        request_id=f"compose:{idempotency_key}",
    )
    return outbound
