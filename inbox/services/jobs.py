from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db.models import F, Q
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DurableJob,
    Message,
    Notification,
    Organization,
    OutboundMessage,
    Report,
)
from inbox.services.ai import classify_message
from inbox.services.domains import expire_unverified_claims, provision_ses_identity
from inbox.services.notifications import (
    create_aging_notifications,
    create_classification_notifications,
    send_pending_email_notification,
)
from inbox.services.outbound import recover_stale_submissions, submit_outbound
from inbox.services.receipt_rules import reconcile_receipt_rule
from inbox.services.reports import (
    daily_report_due,
    generate_report,
    organization_zone,
    schedule_key,
)


def enqueue_job(
    *, kind: str, idempotency_key: str, payload: dict[str, Any], organization=None, due_at=None
) -> DurableJob:
    job, _ = DurableJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "organization": organization,
            "kind": kind,
            "payload": payload,
            "due_at": due_at or timezone.now(),
        },
    )
    return job


def schedule_work(now=None) -> int:
    now = now or timezone.now()
    count = 0
    expire_unverified_claims()
    recover_stale_submissions(now=now)
    for message in Message.objects.filter(
        direction=Message.Direction.INBOUND,
        is_quarantined=False,
        normalized_purged_at__isnull=True,
    ).exclude(classifications__is_current=True):
        enqueue_job(
            kind="classify_message",
            idempotency_key=f"classify:{message.id}",
            payload={"message_id": str(message.id)},
            organization=message.organization,
        )
        count += 1
    for organization in Organization.objects.filter(is_active=True).select_related(
        "report_schedule"
    ):
        schedule = organization.report_schedule
        if not schedule.is_enabled:
            continue
        create_aging_notifications(organization, now=now)
        if schedule.review_frequency == schedule.Frequency.HOURLY:
            scheduled_times: list[datetime] = []
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            if schedule.last_review_at is None:
                scheduled_times.append(now)
            else:
                candidate = schedule.last_review_at.replace(
                    minute=0, second=0, microsecond=0
                ) + timedelta(hours=1)
                while candidate <= current_hour and len(scheduled_times) < 24:
                    scheduled_times.append(candidate)
                    candidate += timedelta(hours=1)
            for scheduled_for in scheduled_times:
                key = schedule_key(organization, Report.Kind.HOURLY, scheduled_for)
                enqueue_job(
                    kind="generate_report",
                    idempotency_key=f"report:{organization.id}:hourly:{key}",
                    payload={
                        "organization_id": str(organization.id),
                        "kind": Report.Kind.HOURLY,
                        "scheduled_for": scheduled_for.isoformat(),
                    },
                    organization=organization,
                )
                count += 1
        if schedule.review_frequency == schedule.Frequency.DAILY and daily_report_due(
            organization, now
        ):
            key = schedule_key(organization, Report.Kind.DAILY, now)
            enqueue_job(
                kind="generate_report",
                idempotency_key=f"report:{organization.id}:daily:{key}",
                payload={
                    "organization_id": str(organization.id),
                    "kind": Report.Kind.DAILY,
                    "scheduled_for": now.isoformat(),
                },
                organization=organization,
            )
            count += 1
    for outbound in OutboundMessage.objects.filter(status=OutboundMessage.Status.QUEUED):
        enqueue_job(
            kind="send_outbound",
            idempotency_key=f"outbound:{outbound.id}",
            payload={"outbound_id": str(outbound.id)},
            organization=outbound.organization,
        )
        count += 1
    for notification in Notification.objects.filter(
        channel=Notification.Channel.EMAIL, status=Notification.Status.PENDING
    ):
        enqueue_job(
            kind="send_notification",
            idempotency_key=f"notification:{notification.id}",
            payload={"notification_id": str(notification.id)},
            organization=notification.organization,
        )
        count += 1
    return count


def _handle(job: DurableJob) -> None:
    if job.kind == "classify_message":
        message = Message.objects.get(id=job.payload["message_id"])
        classification = classify_message(message)
        if classification is None:
            raise RuntimeError("Classification remains unavailable.")
        create_classification_notifications(classification)
    elif job.kind == "generate_report":
        organization = Organization.objects.get(id=job.payload["organization_id"])
        scheduled_for = datetime.fromisoformat(job.payload["scheduled_for"])
        report = generate_report(
            organization=organization,
            kind=job.payload["kind"],
            now=scheduled_for,
        )
        schedule = organization.report_schedule
        if schedule.last_review_at is None or report.period_end > schedule.last_review_at:
            schedule.last_review_at = report.period_end
        if report.kind == Report.Kind.DAILY:
            schedule.last_daily_report_local_date = report.period_end.astimezone(
                organization_zone(organization)
            ).date()
        schedule.save(
            update_fields=("last_review_at", "last_daily_report_local_date", "updated_at")
        )
    elif job.kind == "send_outbound":
        submit_outbound(OutboundMessage.objects.get(id=job.payload["outbound_id"]))
    elif job.kind == "send_notification":
        if not send_pending_email_notification(
            Notification.objects.get(id=job.payload["notification_id"])
        ):
            raise RuntimeError("Notification delivery failed.")
    elif job.kind == "provision_domain":
        from inbox.models import Domain

        provision_ses_identity(Domain.objects.get(id=job.payload["domain_id"]))
    elif job.kind == "dns_check":
        from django.core.management import call_command

        call_command("check_domain_drift")
    elif job.kind == "reconcile_receipt_rule":
        reconcile_receipt_rule()
    else:
        raise ValueError(f"Unsupported job kind: {job.kind}")


def _surface_job_failure(job: DurableJob, *, terminal: bool) -> None:
    if job.kind != "provision_domain":
        return
    domain = (
        Domain.objects.filter(id=job.payload.get("domain_id"))
        .exclude(status=Domain.Status.DISABLED)
        .first()
    )
    if domain is None:
        return
    domain.status = Domain.Status.ERROR if terminal else Domain.Status.PROVISIONING
    domain.error_code = "domain_provision_failed" if terminal else "domain_provision_retry"
    domain.error_message = (
        "SES identity provisioning failed after repeated attempts. Contact support to retry."
        if terminal
        else "SES identity provisioning is temporarily unavailable and will retry automatically."
    )
    domain.save(update_fields=("status", "error_code", "error_message", "updated_at"))
    if terminal:
        AuditEvent.objects.create(
            organization=domain.organization,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.provision_failed",
            object_type="Domain",
            object_id=domain.id,
            request_id=f"job:{job.id}",
            metadata={"attempts": job.attempts},
        )


def run_due_jobs(*, limit: int = 100) -> dict[str, int]:
    counts = {"leased": 0, "complete": 0, "retry": 0, "failed": 0}
    for _ in range(limit):
        now = timezone.now()
        candidate = (
            DurableJob.objects.filter(
                status__in=[
                    DurableJob.Status.PENDING,
                    DurableJob.Status.RETRY,
                    DurableJob.Status.LEASED,
                ],
                due_at__lte=now,
            )
            .filter(Q(leased_until__isnull=True) | Q(leased_until__lte=now))
            .order_by("due_at")
            .values_list("id", flat=True)
            .first()
        )
        if candidate is None:
            break
        lease_until = now + timedelta(minutes=5)
        leased = (
            DurableJob.objects.filter(
                id=candidate,
                status__in=[
                    DurableJob.Status.PENDING,
                    DurableJob.Status.RETRY,
                    DurableJob.Status.LEASED,
                ],
                due_at__lte=now,
            )
            .filter(Q(leased_until__isnull=True) | Q(leased_until__lte=now))
            .update(
                status=DurableJob.Status.LEASED,
                leased_until=lease_until,
                attempts=F("attempts") + 1,
                updated_at=now,
            )
        )
        if not leased:
            continue
        job = DurableJob.objects.get(id=candidate)
        counts["leased"] += 1
        try:
            _handle(job)
        except Exception:
            job.leased_until = None
            if job.attempts >= job.max_attempts:
                job.status = DurableJob.Status.FAILED
                counts["failed"] += 1
                terminal = True
            else:
                job.status = DurableJob.Status.RETRY
                job.due_at = timezone.now() + timedelta(minutes=min(60, 2**job.attempts))
                counts["retry"] += 1
                terminal = False
            job.last_error_code = "job_handler_failed"
            job.save(
                update_fields=(
                    "status",
                    "leased_until",
                    "due_at",
                    "last_error_code",
                    "updated_at",
                )
            )
            _surface_job_failure(job, terminal=terminal)
        else:
            job.status = DurableJob.Status.COMPLETE
            job.leased_until = None
            job.last_error_code = ""
            job.save(update_fields=("status", "leased_until", "last_error_code", "updated_at"))
            counts["complete"] += 1
    return counts
