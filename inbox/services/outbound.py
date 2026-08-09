from __future__ import annotations

from datetime import timedelta
from email.message import EmailMessage
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
)
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from inbox.models import AuditEvent, Domain, DraftApproval, Message, OutboundMessage


def recover_stale_submissions(*, now=None, stale_after_minutes: int = 10) -> int:
    """Never retry a send whose SES acceptance became unknowable after a crash."""
    now = now or timezone.now()
    return OutboundMessage.objects.filter(
        status=OutboundMessage.Status.SUBMITTING,
        updated_at__lte=now - timedelta(minutes=stale_after_minutes),
    ).update(
        status=OutboundMessage.Status.UNKNOWN,
        failed_at=now,
        error_code="ses_acceptance_unknown",
        error_message=(
            "The sender stopped while submitting. Automatic retry is disabled; "
            "the owner must explicitly resend."
        ),
        updated_at=now,
    )


def _authorization_error(outbound: OutboundMessage) -> str:
    approval = DraftApproval.objects.filter(
        revision=outbound.revision,
        invalidated_at__isnull=True,
        content_hash=outbound.content_hash,
    ).first()
    draft = outbound.revision.draft
    if approval is None or draft.current_revision_id != outbound.revision_id or draft.is_stale:
        return "The exact-revision approval is no longer active."
    sender_domain = outbound.from_address.rsplit("@", 1)[-1].casefold()
    domain = Domain.objects.filter(id=outbound.domain_id, hostname=sender_domain).first()
    if (
        domain is None
        or domain.status == Domain.Status.DISABLED
        or not domain.outbound_ready
        or not domain.ownership_verified
    ):
        return "Outbound sending is no longer ready for this domain."
    if not outbound.domain.owner.is_active:
        return "The domain owner is not active."
    return ""


def _raw_message(outbound: OutboundMessage) -> bytes:
    message = EmailMessage()
    message["From"] = outbound.from_address
    message["To"] = outbound.to_address
    message["Subject"] = outbound.subject
    message["Message-ID"] = outbound.rfc_message_id
    context = outbound.revision.draft.context_message
    if context.rfc_message_id:
        message["In-Reply-To"] = context.rfc_message_id
        # Raw reference values are not persisted; preserve the immediate parent ID.
        message["References"] = context.rfc_message_id
    message.set_content(outbound.body_text)
    return message.as_bytes()


def submit_outbound(outbound: OutboundMessage, *, ses_client: Any | None = None) -> OutboundMessage:
    with transaction.atomic():
        locked = (
            OutboundMessage.objects.select_for_update()
            .select_related("domain__owner", "revision__draft")
            .get(id=outbound.id)
        )
        if locked.status != OutboundMessage.Status.QUEUED:
            return locked
        authorization_error = _authorization_error(locked)
        if authorization_error:
            locked.status = OutboundMessage.Status.FAILED
            locked.failed_at = timezone.now()
            locked.error_code = "send_authorization_revoked"
            locked.error_message = authorization_error
            locked.save(
                update_fields=(
                    "status",
                    "failed_at",
                    "error_code",
                    "error_message",
                    "updated_at",
                )
            )
            return locked
        locked.status = OutboundMessage.Status.SUBMITTING
        locked.save(update_fields=("status", "updated_at"))
    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    provider_message_id = ""
    target_status = OutboundMessage.Status.UNKNOWN
    error_code = "ses_acceptance_unknown"
    error_message = "SES acceptance could not be determined. Automatic retry is disabled."
    accepted_at = None
    try:
        response = client.send_raw_email(
            Source=locked.from_address,
            Destinations=[locked.to_address],
            RawMessage={"Data": _raw_message(locked)},
            ConfigurationSetName=settings.AWS_SES_CONFIGURATION_SET,
            Tags=[{"Name": "outbound_id", "Value": str(locked.id)}],
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "ses_error"))
        status_code = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if status_code >= 500:
            target_status = OutboundMessage.Status.UNKNOWN
            error_code = "ses_acceptance_unknown"
            error_message = "SES may have accepted this message. Automatic retry is disabled."
        else:
            target_status = OutboundMessage.Status.FAILED
            error_code = code[:64]
            error_message = "SES rejected the send before acceptance."
    except BotoCoreError:
        # Connection, TLS and read failures may happen after SES accepted the bytes.
        # Treat every transport-level outcome as ambiguous and require an explicit resend.
        pass
    else:
        provider_message_id = str(response["MessageId"])
        accepted_at = timezone.now()
        target_status = OutboundMessage.Status.ACCEPTED
        error_code = ""
        error_message = ""

    with transaction.atomic():
        current = (
            OutboundMessage.objects.select_for_update()
            .select_related("domain", "conversation", "revision__draft")
            .get(id=locked.id)
        )
        update_fields: set[str] = set()
        if provider_message_id:
            if not current.provider_message_id:
                current.provider_message_id = provider_message_id
                update_fields.add("provider_message_id")
            if current.accepted_at is None:
                current.accepted_at = accepted_at
                update_fields.add("accepted_at")
        elif (
            current.provider_message_id
            and current.accepted_at is None
            and current.status
            in {
                OutboundMessage.Status.ACCEPTED,
                OutboundMessage.Status.DELIVERED,
                OutboundMessage.Status.BOUNCED,
                OutboundMessage.Status.COMPLAINED,
            }
        ):
            # A tagged delivery event can prove acceptance while the SES call is
            # still returning an ambiguous transport outcome.
            current.accepted_at = current.delivered_at or current.failed_at or timezone.now()
            update_fields.add("accepted_at")
        if current.status == OutboundMessage.Status.SUBMITTING:
            current.status = target_status
            current.error_code = error_code
            current.error_message = error_message
            update_fields.update(("status", "error_code", "error_message"))
            if target_status in {OutboundMessage.Status.FAILED, OutboundMessage.Status.UNKNOWN}:
                current.failed_at = timezone.now()
                update_fields.add("failed_at")
        if update_fields:
            update_fields.add("updated_at")
            current.save(update_fields=tuple(sorted(update_fields)))
        AuditEvent.objects.create(
            domain=current.domain,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="outbound.submission_finished",
            object_type="OutboundMessage",
            object_id=current.id,
            request_id=f"outbound:{current.id}",
            metadata={"status": current.status, "error_code": current.error_code},
        )

    if current.provider_message_id:
        Message.objects.get_or_create(
            domain=current.domain,
            provider_message_id=current.provider_message_id,
            defaults={
                "conversation": current.conversation,
                "direction": Message.Direction.OUTBOUND,
                "rfc_message_id": current.rfc_message_id,
                "from_address": current.from_address,
                "subject": current.subject,
                "text_body": current.body_text,
                "received_at": current.accepted_at or accepted_at or timezone.now(),
            },
        )
        if current.status in {OutboundMessage.Status.ACCEPTED, OutboundMessage.Status.DELIVERED}:
            outbound_at = current.accepted_at or accepted_at or timezone.now()
            with transaction.atomic():
                conversation = current.conversation.__class__.objects.select_for_update().get(
                    id=current.conversation_id
                )
                conversation.last_outbound_at = outbound_at
                conversation.save(update_fields=("last_outbound_at", "updated_at"))
    return current
