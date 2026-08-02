from __future__ import annotations

from datetime import timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from inbox.models import (
    Attachment,
    AuditEvent,
    Classification,
    Conversation,
    DeliveryEvent,
    Domain,
    DraftApproval,
    DurableJob,
    EmailVerificationToken,
    IngressEvent,
    Message,
    MessageRecipient,
    Notification,
    OutboundMessage,
    ReplyDraftRevision,
    Report,
    ReportItem,
    RetentionPolicy,
    SignupAttempt,
    content_digest,
)
from inbox.services.entitlements import for_user


def _delete_s3_key(client: Any, key: str) -> None:
    if not key:
        return
    try:
        client.delete_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
            raise


def _default_retention_days(field_name: str) -> int:
    return int(getattr(RetentionPolicy(), field_name))


def _effective_retention_days(domain: Domain, policy: RetentionPolicy, field_name: str) -> int:
    configured = int(getattr(policy, field_name))
    if for_user(domain.owner).custom_settings:
        return configured
    return min(configured, _default_retention_days(field_name))


def _purge_ingress_events(*, raw_cutoff: Any, metadata_cutoff: Any, now: Any) -> tuple[int, int]:
    events = IngressEvent.objects.all()
    expired_metadata = events.filter(created_at__lte=metadata_cutoff)
    metadata_count = expired_metadata.count()
    expired_metadata.delete()
    raw_count = (
        events.filter(created_at__lte=raw_cutoff)
        .exclude(source_bucket="", source_key="")
        .update(source_bucket="", source_key="", updated_at=now)
    )
    return raw_count, metadata_count


def _purge_terminal_jobs(*, domain: Domain | None, cutoff: Any) -> int:
    jobs = DurableJob.objects.filter(
        domain=domain,
        status__in=[DurableJob.Status.COMPLETE, DurableJob.Status.FAILED],
        updated_at__lte=cutoff,
    )
    count = jobs.count()
    jobs.delete()
    return count


def purge_retention(*, s3_client: Any | None = None, now=None) -> dict[str, int]:
    now = now or timezone.now()
    s3 = s3_client or boto3.client("s3", region_name=settings.AWS_REGION)
    counts = {
        "attachments": 0,
        "raw": 0,
        "normalized": 0,
        "audit": 0,
        "delivery": 0,
        "ingress_raw": 0,
        "ingress_metadata": 0,
        "signup_attempts": 0,
        "verification_tokens": 0,
        "terminal_jobs": 0,
    }
    domains = list(Domain.objects.select_related("owner").all())
    domain_policies: list[tuple[Domain, RetentionPolicy]] = []
    for domain in domains:
        policy, _ = RetentionPolicy.objects.get_or_create(domain=domain)
        domain_policies.append((domain, policy))

    for domain, policy in domain_policies:
        attachment_days = _effective_retention_days(domain, policy, "attachment_days")
        raw_message_days = _effective_retention_days(domain, policy, "raw_message_days")
        normalized_content_days = _effective_retention_days(
            domain, policy, "normalized_content_days"
        )
        audit_metadata_days = _effective_retention_days(domain, policy, "audit_metadata_days")
        delivery_metadata_days = _effective_retention_days(domain, policy, "delivery_metadata_days")
        attachment_cutoff = now - timedelta(days=attachment_days)
        for attachment in Attachment.objects.filter(
            domain=domain,
            purged_at__isnull=True,
            message__received_at__lte=attachment_cutoff,
        ).iterator():
            _delete_s3_key(s3, attachment.s3_key)
            attachment.s3_key = ""
            attachment.purged_at = now
            attachment.scan_status = Attachment.ScanStatus.EXPIRED
            attachment.display_name = "expired attachment"
            attachment.content_type = "application/octet-stream"
            attachment.detected_content_type = ""
            attachment.size = 0
            attachment.sha256 = ""
            attachment.save(
                update_fields=(
                    "s3_key",
                    "purged_at",
                    "scan_status",
                    "display_name",
                    "content_type",
                    "detected_content_type",
                    "size",
                    "sha256",
                    "updated_at",
                )
            )
            counts["attachments"] += 1
        raw_cutoff = now - timedelta(days=raw_message_days)
        for message in (
            Message.objects.filter(
                domain=domain,
                raw_purged_at__isnull=True,
                received_at__lte=raw_cutoff,
            )
            .exclude(raw_s3_key="")
            .iterator()
        ):
            _delete_s3_key(s3, message.raw_s3_key)
            message.raw_s3_key = ""
            message.raw_purged_at = now
            message.save(update_fields=("raw_s3_key", "raw_purged_at", "updated_at"))
            counts["raw"] += 1
        normalized_cutoff = now - timedelta(days=normalized_content_days)
        old_messages = list(
            Message.objects.filter(
                domain=domain,
                normalized_purged_at__isnull=True,
                received_at__lte=normalized_cutoff,
            )
        )
        old_message_ids = [message.id for message in old_messages]
        for message in old_messages:
            message.subject = ""
            message.from_address = "redacted@invalid.local"
            message.reply_to_address = ""
            message.rfc_message_id = ""
            message.text_body = ""
            message.html_body = ""
            message.normalized_purged_at = now
            message.save(
                update_fields=(
                    "subject",
                    "from_address",
                    "reply_to_address",
                    "rfc_message_id",
                    "text_body",
                    "html_body",
                    "normalized_purged_at",
                    "updated_at",
                )
            )
            counts["normalized"] += 1
        if old_message_ids:
            MessageRecipient.objects.filter(message_id__in=old_message_ids).delete()
            Classification.objects.filter(message_id__in=old_message_ids).update(
                topic="", summary="", recommended_action=""
            )
        Conversation.objects.filter(domain=domain).exclude(
            messages__normalized_purged_at__isnull=True
        ).update(subject="", normalized_subject="")

        redacted_hash = content_digest("", "")
        old_revisions = ReplyDraftRevision.objects.filter(
            domain=domain, created_at__lte=normalized_cutoff
        )
        DraftApproval.objects.filter(
            revision__in=old_revisions, invalidated_at__isnull=True
        ).update(
            invalidated_at=now,
            invalidated_reason="retention_expired",
        )
        DraftApproval.objects.filter(revision__in=old_revisions).update(content_hash=redacted_hash)
        # QuerySet.update is the explicit retention-only exception to immutable
        # revisions; all related hashes are redacted together.
        old_revisions.update(subject="", body_text="", content_hash=redacted_hash)
        old_outbound = OutboundMessage.objects.filter(
            domain=domain, created_at__lte=normalized_cutoff
        )
        old_outbound.filter(
            status__in=[OutboundMessage.Status.QUEUED, OutboundMessage.Status.SUBMITTING]
        ).update(
            status=OutboundMessage.Status.FAILED,
            failed_at=now,
            error_code="retention_expired",
            error_message="The approved content expired before submission.",
        )
        old_outbound.update(
            from_address="redacted@invalid.local",
            to_address="redacted@invalid.local",
            subject="",
            body_text="",
            content_hash=redacted_hash,
        )
        report_cutoff = now - timedelta(days=normalized_content_days)
        old_reports = Report.objects.filter(domain=domain, period_end__lte=report_cutoff)
        ReportItem.objects.filter(report__in=old_reports).update(summary="")
        old_reports.update(title="Expired report", content="")
        Notification.objects.filter(domain=domain, created_at__lte=normalized_cutoff).update(
            title="Expired notification", body=""
        )
        audit_cutoff = now - timedelta(days=audit_metadata_days)
        deleted, _ = AuditEvent.all_objects.filter(
            domain=domain, created_at__lte=audit_cutoff
        ).delete()
        counts["audit"] += deleted
        delivery_cutoff = now - timedelta(days=delivery_metadata_days)
        deleted, _ = DeliveryEvent.objects.filter(
            domain=domain, occurred_at__lte=delivery_cutoff
        ).delete()
        counts["delivery"] += deleted
        counts["terminal_jobs"] += _purge_terminal_jobs(
            domain=domain,
            cutoff=audit_cutoff,
        )
        AuditEvent.objects.create(
            domain=domain,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="retention.completed",
            object_type="Domain",
            object_id=domain.id,
            request_id=f"retention:{now:%Y%m%d}",
            metadata={key: value for key, value in counts.items()},
        )

    # Events and jobs without a single tenant include malformed, unroutable, and
    # multi-tenant ingress. Preserve them until the longest configured policy (or
    # the model default) has elapsed so a short tenant policy cannot erase another
    # tenant's operational metadata early.
    global_raw_days = max(
        [_default_retention_days("raw_message_days")]
        + [
            _effective_retention_days(domain, policy, "raw_message_days")
            for domain, policy in domain_policies
        ]
    )
    global_metadata_days = max(
        [_default_retention_days("audit_metadata_days")]
        + [
            _effective_retention_days(domain, policy, "audit_metadata_days")
            for domain, policy in domain_policies
        ]
    )
    ingress_raw, ingress_metadata = _purge_ingress_events(
        raw_cutoff=now - timedelta(days=global_raw_days),
        metadata_cutoff=now - timedelta(days=global_metadata_days),
        now=now,
    )
    counts["ingress_raw"] += ingress_raw
    counts["ingress_metadata"] += ingress_metadata
    counts["terminal_jobs"] += _purge_terminal_jobs(
        domain=None,
        cutoff=now - timedelta(days=global_metadata_days),
    )

    attempt_windows = {
        SignupAttempt.Kind.SIGNUP: settings.SIGNUP_RATE_WINDOW_SECONDS,
        SignupAttempt.Kind.VERIFICATION_RESEND: settings.VERIFICATION_RESEND_RATE_WINDOW_SECONDS,
    }
    for kind, window_seconds in attempt_windows.items():
        cutoff = now - timedelta(seconds=max(window_seconds, 0))
        deleted, _ = SignupAttempt.objects.filter(kind=kind, created_at__lt=cutoff).delete()
        counts["signup_attempts"] += deleted
    deleted, _ = EmailVerificationToken.objects.filter(
        Q(expires_at__lte=now) | Q(used_at__isnull=False)
    ).delete()
    counts["verification_tokens"] += deleted
    return counts
