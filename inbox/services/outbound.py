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
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DraftApproval,
    DurableJob,
    Message,
    OutboundControl,
    OutboundMessage,
    User,
)
from inbox.services.notifications import create_outbound_problem_notifications


def get_outbound_control(user: User, *, lock: bool = False) -> OutboundControl:
    if lock:
        control, _ = OutboundControl.objects.get_or_create(user=user)
        return OutboundControl.objects.select_for_update().get(id=control.id)
    return OutboundControl.objects.filter(user=user).first() or OutboundControl(user=user)


def outbound_usage(user: User, *, now=None) -> dict[str, Any]:
    now = now or timezone.now()
    base = OutboundMessage.objects.filter(domain__owner=user)
    day_start = now - timedelta(hours=24)
    minute_start = now - timedelta(minutes=1)
    by_domain = {
        str(row["domain_id"]): row["count"]
        for row in (
            base.filter(created_at__gte=day_start).values("domain_id").annotate(count=Count("id"))
        )
    }
    return {
        "minute": base.filter(created_at__gte=minute_start).count(),
        "day": base.filter(created_at__gte=day_start).count(),
        "by_domain": by_domain,
        "limits": {
            "minute": settings.OUTBOUND_RATE_LIMIT_PER_MINUTE,
            "day": settings.OUTBOUND_DAILY_ACCOUNT_LIMIT,
            "domain_day": settings.OUTBOUND_DAILY_DOMAIN_LIMIT,
        },
    }


def require_outbound_capacity(domain: Domain, *, now=None) -> None:
    """Serialize account sends and reject paused or over-limit queue requests."""
    now = now or timezone.now()
    control = get_outbound_control(domain.owner, lock=True)
    if control.is_paused:
        raise ValidationError(
            "Outbound sending is paused for this account.", code="outbound_paused"
        )
    base = OutboundMessage.objects.filter(domain__owner=domain.owner)
    if (
        settings.OUTBOUND_RATE_LIMIT_PER_MINUTE > 0
        and base.filter(created_at__gte=now - timedelta(minutes=1)).count()
        >= settings.OUTBOUND_RATE_LIMIT_PER_MINUTE
    ):
        raise ValidationError(
            "The account send rate limit has been reached. Try again shortly.",
            code="outbound_rate_limited",
        )
    if (
        settings.OUTBOUND_DAILY_ACCOUNT_LIMIT > 0
        and base.filter(created_at__gte=now - timedelta(hours=24)).count()
        >= settings.OUTBOUND_DAILY_ACCOUNT_LIMIT
    ):
        raise ValidationError(
            "The account daily send limit has been reached.",
            code="outbound_account_limit",
        )
    if (
        settings.OUTBOUND_DAILY_DOMAIN_LIMIT > 0
        and base.filter(domain=domain, created_at__gte=now - timedelta(hours=24)).count()
        >= settings.OUTBOUND_DAILY_DOMAIN_LIMIT
    ):
        raise ValidationError(
            "The domain daily send limit has been reached.",
            code="outbound_domain_limit",
        )


@transaction.atomic
def set_outbound_paused(user: User, *, paused: bool) -> OutboundControl:
    control = get_outbound_control(user, lock=True)
    if control.is_paused == paused:
        return control
    control.is_paused = paused
    control.paused_at = timezone.now() if paused else None
    control.save(update_fields=("is_paused", "paused_at", "updated_at"))
    if not paused:
        queued_ids = OutboundMessage.objects.filter(
            domain__owner=user, status=OutboundMessage.Status.QUEUED
        ).values_list("id", flat=True)
        DurableJob.objects.filter(
            idempotency_key__in=[f"outbound:{value}" for value in queued_ids]
        ).exclude(status=DurableJob.Status.LEASED).update(
            status=DurableJob.Status.PENDING,
            due_at=timezone.now(),
            leased_until=None,
            last_error_code="",
            updated_at=timezone.now(),
        )
    return control


def recover_stale_submissions(*, now=None, stale_after_minutes: int = 10) -> int:
    """Never retry a send whose SES acceptance became unknowable after a crash."""
    now = now or timezone.now()
    stale = list(
        OutboundMessage.objects.filter(
            status=OutboundMessage.Status.SUBMITTING,
            updated_at__lte=now - timedelta(minutes=stale_after_minutes),
        ).select_related("domain", "conversation")
    )
    for outbound in stale:
        outbound.status = OutboundMessage.Status.UNKNOWN
        outbound.failed_at = now
        outbound.error_code = "ses_acceptance_unknown"
        outbound.error_message = (
            "The sender stopped while submitting. Automatic retry is disabled; "
            "the owner must explicitly resend."
        )
        outbound.save(
            update_fields=("status", "failed_at", "error_code", "error_message", "updated_at")
        )
        create_outbound_problem_notifications(outbound)
    return len(stale)


def _authorization_error(outbound: OutboundMessage) -> str:
    draft = outbound.revision.draft
    if draft.current_revision_id != outbound.revision_id or draft.is_stale:
        return "The exact draft revision is no longer current."
    if outbound.authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL:
        approval = DraftApproval.objects.filter(
            revision=outbound.revision,
            invalidated_at__isnull=True,
            content_hash=outbound.content_hash,
        ).first()
        if approval is None:
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
        control = OutboundControl.objects.filter(user=locked.domain.owner).first()
        if control is not None and control.is_paused:
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
            create_outbound_problem_notifications(locked)
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

    if current.status in {OutboundMessage.Status.FAILED, OutboundMessage.Status.UNKNOWN}:
        create_outbound_problem_notifications(current)

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
