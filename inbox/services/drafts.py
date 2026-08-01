from __future__ import annotations

from typing import Any

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
from inbox.services.ai import generate_draft_output


def create_draft(message: Message, *, client: Any | None = None) -> ReplyDraft:
    if (
        message.is_quarantined
        or message.conversation.status == message.conversation.Status.QUARANTINED
    ):
        raise ValidationError("A reply draft cannot be generated for quarantined content.")
    output = generate_draft_output(message, client=client)
    with transaction.atomic():
        draft = ReplyDraft.objects.create(
            organization=message.organization,
            project=message.project,
            conversation=message.conversation,
            context_message=message,
        )
        revision = ReplyDraftRevision(
            organization=message.organization,
            draft=draft,
            number=1,
            subject=output.subject,
            body_text=output.body_text,
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
    locked = (
        ReplyDraft.objects.select_for_update().select_related("current_revision").get(id=draft.id)
    )
    if locked.organization.owner_id != owner.id:
        raise ValidationError("Only the organization owner can edit a draft.")
    current_revision = locked.current_revision
    if current_revision is None:
        next_number = 1
    else:
        next_number = current_revision.number + 1
        DraftApproval.objects.filter(revision=current_revision, invalidated_at__isnull=True).update(
            invalidated_at=timezone.now(), invalidated_reason="superseded_by_edit"
        )
    revision = ReplyDraftRevision(
        organization=locked.organization,
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


@transaction.atomic
def approve_exact_revision(
    *, draft: ReplyDraft, revision_id: object, content_hash: str, owner: User
) -> OutboundMessage:
    locked = (
        ReplyDraft.objects.select_for_update()
        .select_related("organization", "current_revision", "context_message", "conversation")
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
    if locked.organization.owner_id != owner.id:
        raise ValidationError("Only the organization owner can approve a reply.")
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
            organization=locked.organization,
            revision=revision,
            approved_by=owner,
            content_hash=content_hash,
        )
        approval.full_clean()
        approval.save()
    recipient = locked.context_message.reply_to_address or locked.context_message.from_address
    domain = locked.context_message.recipients.filter(is_routing_recipient=True).first()
    if domain is None:
        raise ValidationError("The original routing recipient is unavailable.")
    local_part, recipient_domain = domain.address.rsplit("@", 1)
    sending_domain = Domain.objects.filter(
        organization=locked.organization,
        project=locked.project,
        hostname=recipient_domain,
    ).first()
    if sending_domain is None:
        route = (
            InboundRoute.objects.filter(
                organization=locked.organization,
                address=domain.address,
                domain__project=locked.project,
            )
            .select_related("domain")
            .first()
        )
        sending_domain = route.domain if route else None
        local_part = "reply"
    if sending_domain is None or not sending_domain.outbound_ready:
        raise ValidationError("Outbound sending is not ready for this domain.")
    from_address = f"{local_part}@{sending_domain.hostname}"
    outbound = OutboundMessage(
        organization=locked.organization,
        project=locked.project,
        conversation=locked.conversation,
        revision=revision,
        from_address=from_address,
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
def resend_outbound(original: OutboundMessage, *, owner: User) -> OutboundMessage:
    if original.organization.owner_id != owner.id:
        raise ValidationError("Only the organization owner can resend a message.")
    if original.status not in {OutboundMessage.Status.FAILED, OutboundMessage.Status.UNKNOWN}:
        raise ValidationError("Only failed or unknown sends can be resent explicitly.")
    attempt = original.revision.outbound_messages.count() + 1
    resend = OutboundMessage(
        organization=original.organization,
        project=original.project,
        conversation=original.conversation,
        revision=original.revision,
        parent=original,
        attempt_number=attempt,
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
