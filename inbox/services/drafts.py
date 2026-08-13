from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inbox.models import (
    Domain,
    DraftApproval,
    InboundRoute,
    Message,
    OutboundMessage,
    ReplyDraft,
    ReplyDraftRevision,
    User,
)
from inbox.services.entitlements import can_manage_domain
from inbox.services.outbound import require_outbound_capacity


def create_authored_draft(
    *, message: Message, owner: User, subject: str, body_text: str
) -> ReplyDraft:
    if message.domain.owner_id != owner.id:
        raise ValidationError("Only the domain owner can create a reply draft.")
    if not can_manage_domain(owner, message.domain):
        raise ValidationError(
            "This domain is read-only while the account exceeds its active-domain capacity.",
            code="domain_read_only",
        )
    if message.is_quarantined:
        raise ValidationError("A reply draft cannot be created for quarantined content.")
    with transaction.atomic():
        draft = ReplyDraft.objects.create(
            domain=message.domain,
            conversation=message.conversation,
            context_message=message,
        )
        revision = ReplyDraftRevision(
            domain=message.domain,
            draft=draft,
            number=1,
            subject=subject,
            body_text=body_text,
            author=owner,
            is_agent_generated=True,
        )
        revision.full_clean()
        revision.save()
        draft.current_revision = revision
        draft.save(update_fields=("current_revision", "updated_at"))
    return draft


@transaction.atomic
def revise_draft(
    *, draft: ReplyDraft, owner: User, subject: str, body_text: str
) -> ReplyDraftRevision:
    locked = ReplyDraft.objects.select_for_update().select_related("domain").get(id=draft.id)
    if locked.domain.owner_id != owner.id:
        raise ValidationError("Only the domain owner can edit a draft.")
    if not can_manage_domain(owner, locked.domain):
        raise ValidationError(
            "This domain is read-only while the account exceeds its active-domain capacity.",
            code="domain_read_only",
        )
    current_revision = locked.current_revision
    if current_revision is None:
        next_number = 1
    else:
        next_number = current_revision.number + 1
        DraftApproval.objects.filter(revision=current_revision, invalidated_at__isnull=True).update(
            invalidated_at=timezone.now(), invalidated_reason="superseded_by_edit"
        )
    revision = ReplyDraftRevision(
        domain=locked.domain,
        draft=locked,
        number=next_number,
        subject=subject,
        body_text=body_text,
        author=owner,
    )
    revision.full_clean()
    revision.save()
    locked.current_revision = revision
    locked.save(update_fields=("current_revision", "updated_at"))
    return revision


def _outbound_for_revision(
    *,
    draft: ReplyDraft,
    revision: ReplyDraftRevision,
    authorization_mode: str,
) -> OutboundMessage:
    recipient = draft.context_message.reply_to_address or draft.context_message.from_address
    routing_recipient = draft.context_message.recipients.filter(is_routing_recipient=True).first()
    if routing_recipient is None:
        raise ValidationError("The original routing recipient is unavailable.")
    local_part, recipient_domain = routing_recipient.address.rsplit("@", 1)
    sending_domain = Domain.objects.filter(id=draft.domain_id, hostname=recipient_domain).first()
    if sending_domain is None:
        route = (
            InboundRoute.objects.filter(domain=draft.domain, address=routing_recipient.address)
            .select_related("domain")
            .first()
        )
        sending_domain = route.domain if route else None
        local_part = "reply"
    if sending_domain is None or not sending_domain.outbound_ready:
        raise ValidationError("Outbound sending is not ready for this domain.")
    require_outbound_capacity(draft.domain)
    outbound = OutboundMessage(
        domain=draft.domain,
        conversation=draft.conversation,
        revision=revision,
        authorization_mode=authorization_mode,
        from_address=f"{local_part}@{sending_domain.hostname}",
        to_address=recipient,
        subject=revision.subject,
        body_text=revision.body_text,
        content_hash=revision.content_hash,
        rfc_message_id=f"<{OutboundMessage._meta.pk.get_default()}@operationalinbox.com>",
    )
    outbound.full_clean()
    outbound.save()
    return outbound


@transaction.atomic
def approve_exact_revision(
    *, draft: ReplyDraft, revision_id: object, content_hash: str, owner: User
) -> OutboundMessage:
    locked = (
        ReplyDraft.objects.select_for_update()
        .select_related("domain", "context_message", "conversation")
        .get(id=draft.id)
    )
    revision = locked.current_revision
    if revision is None or revision.id != revision_id:
        raise ValidationError({"revision": "The draft changed. Review the current revision."})
    if locked.is_stale:
        raise ValidationError("A newer inbound message made this draft stale.")
    if revision.content_hash != content_hash:
        raise ValidationError(
            {"content_hash": "The draft changed. Review the exact content again."}
        )
    if locked.domain.owner_id != owner.id:
        raise ValidationError("Only the domain owner can approve a reply.")
    if not can_manage_domain(owner, locked.domain):
        raise ValidationError(
            "This domain is read-only while the account exceeds its active-domain capacity.",
            code="domain_read_only",
        )
    approval = DraftApproval.objects.filter(revision=revision).first()
    if approval is not None:
        if (
            approval.invalidated_at is not None
            or approval.content_hash != content_hash
            or approval.approved_by_id != owner.id
        ):
            raise ValidationError("The existing approval is no longer valid.")
        existing_outbound = revision.outbound_messages.order_by("attempt_number").first()
        if existing_outbound is not None:
            return existing_outbound
    else:
        approval = DraftApproval(
            domain=locked.domain,
            revision=revision,
            approved_by=owner,
            content_hash=content_hash,
        )
        approval.full_clean()
        approval.save()
    return _outbound_for_revision(
        draft=locked,
        revision=revision,
        authorization_mode=OutboundMessage.AuthorizationMode.OWNER_APPROVAL,
    )


@transaction.atomic
def send_exact_revision(
    *, draft: ReplyDraft, revision_id: object, content_hash: str, owner: User
) -> OutboundMessage:
    """Queue an exact agent-authored revision under an already-delegated send scope."""
    locked = (
        ReplyDraft.objects.select_for_update()
        .select_related("domain", "context_message", "conversation")
        .get(id=draft.id)
    )
    revision = locked.current_revision
    if revision is None or revision.id != revision_id:
        raise ValidationError({"revision": "The draft changed. Read the current revision."})
    if locked.is_stale:
        raise ValidationError("A newer inbound message made this draft stale.")
    if revision.content_hash != content_hash:
        raise ValidationError({"content_hash": "The draft changed. Read the exact content again."})
    if locked.domain.owner_id != owner.id:
        raise ValidationError("The delegated send scope does not cover this draft.")
    if not can_manage_domain(owner, locked.domain):
        raise ValidationError(
            "This domain is read-only while the account exceeds its active-domain capacity.",
            code="domain_read_only",
        )
    existing = revision.outbound_messages.order_by("attempt_number").first()
    if existing is not None:
        return existing
    return _outbound_for_revision(
        draft=locked,
        revision=revision,
        authorization_mode=OutboundMessage.AuthorizationMode.DELEGATED_SCOPE,
    )


@transaction.atomic
def resend_outbound(original: OutboundMessage, *, owner: User) -> OutboundMessage:
    if original.domain.owner_id != owner.id:
        raise ValidationError("Only the domain owner can resend a message.")
    if not can_manage_domain(owner, original.domain):
        raise ValidationError(
            "This domain is read-only while the account exceeds its active-domain capacity.",
            code="domain_read_only",
        )
    if original.status not in {OutboundMessage.Status.FAILED, OutboundMessage.Status.UNKNOWN}:
        raise ValidationError("Only failed or unknown sends can be resent explicitly.")
    require_outbound_capacity(original.domain)
    attempt = original.revision.outbound_messages.count() + 1
    resend = OutboundMessage(
        domain=original.domain,
        conversation=original.conversation,
        revision=original.revision,
        parent=original,
        attempt_number=attempt,
        authorization_mode=original.authorization_mode,
        from_address=original.from_address,
        to_address=original.to_address,
        subject=original.subject,
        body_text=original.body_text,
        content_hash=original.content_hash,
        rfc_message_id=f"<{OutboundMessage._meta.pk.get_default()}@operationalinbox.com>",
    )
    resend.full_clean()
    resend.save()
    return resend
