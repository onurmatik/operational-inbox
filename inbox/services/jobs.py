from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DurableJob,
    InboundRoute,
    InboundRoutingTransition,
    Message,
    Notification,
    OutboundControl,
    OutboundMessage,
    Report,
)
from inbox.services.ai import classify_message
from inbox.services.domains import (
    expire_unverified_claims,
    provision_inbound,
    provision_outbound_identity,
)
from inbox.services.entitlements import for_user
from inbox.services.notifications import send_pending_email_notification
from inbox.services.outbound import recover_stale_submissions, submit_outbound
from inbox.services.receipt_rules import reconcile_receipt_rule
from inbox.services.reports import (
    domain_zone,
    generate_report,
)
from inbox.services.routing_transitions import (
    complete_expired_routing_transition,
    provision_routing_transition,
)

RETRYABLE_DOMAIN_PROVISION_ERROR_CODES = frozenset(
    {
        "domain_provision_failed",
        # Legacy releases treated an existing account-scoped SES identity as
        # terminal. Current provisioning keeps it unchanged and requires a new,
        # claim-bound DNS ownership proof before adoption.
        "ses_identity_collision",
    }
)


def enqueue_job(
    *, kind: str, idempotency_key: str, payload: dict[str, Any], domain=None, due_at=None
) -> DurableJob:
    job, _ = DurableJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "domain": domain,
            "kind": kind,
            "payload": payload,
            "due_at": due_at or timezone.now(),
        },
    )
    return job


def _inbound_job_generation(job: DurableJob) -> int:
    return int(job.payload.get("setup_generation", 1))


def _active_inbound_provision_job(domain: Domain) -> DurableJob | None:
    active_jobs = DurableJob.objects.filter(
        kind="provision_domain",
        payload__domain_id=str(domain.id),
        status__in=[
            DurableJob.Status.PENDING,
            DurableJob.Status.LEASED,
            DurableJob.Status.RETRY,
        ],
    ).order_by("created_at")
    return next(
        (
            job
            for job in active_jobs
            if _inbound_job_generation(job) == domain.inbound_setup_generation
        ),
        None,
    )


def can_retry_domain_provisioning(domain: Domain) -> bool:
    return domain.status == Domain.Status.ERROR and (
        domain.error_code in RETRYABLE_DOMAIN_PROVISION_ERROR_CODES
    )


def retry_domain_provisioning(domain: Domain) -> tuple[Domain, DurableJob, bool]:
    """Start one recoverable provisioning attempt, or return the active one."""

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        retry_generation = locked_domain.updated_at.strftime("%Y%m%d%H%M%S%f")
        if locked_domain.status == Domain.Status.PROVISIONING:
            active_job = _active_inbound_provision_job(locked_domain)
            if active_job is not None:
                return locked_domain, active_job, False
        elif can_retry_domain_provisioning(locked_domain):
            locked_domain.status = Domain.Status.PROVISIONING
            locked_domain.inbound_ready = False
            locked_domain.error_code = ""
            locked_domain.error_message = ""
            locked_domain.save(
                update_fields=(
                    "status",
                    "inbound_ready",
                    "error_code",
                    "error_message",
                    "updated_at",
                )
            )
        else:
            raise ValidationError("This domain setup is not eligible for a provisioning retry.")

        job = enqueue_job(
            kind="provision_domain",
            idempotency_key=(f"provision-domain:{locked_domain.id}:retry:{retry_generation}"),
            payload={
                "domain_id": str(locked_domain.id),
                "setup_generation": locked_domain.inbound_setup_generation,
                "setup_mode": locked_domain.setup_mode,
            },
            domain=locked_domain,
        )
        return locked_domain, job, True


def can_switch_domain_to_direct(domain: Domain) -> bool:
    return (
        domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD
        and domain.status
        in {
            Domain.Status.PROVISIONING,
            Domain.Status.PENDING_DNS,
            Domain.Status.ERROR,
        }
        and not domain.ownership_verified
        and not domain.inbound_ready
        and not domain.outbound_ready
        and domain.outbound_status == Domain.OutboundStatus.DISABLED
        and not domain.tests.exists()
    )


def switch_domain_to_direct(domain: Domain) -> tuple[Domain, DurableJob, bool]:
    """Restart an unverified provider-forward setup as a fenced direct-MX attempt."""

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        if locked_domain.setup_mode == Domain.SetupMode.DIRECT_MX:
            active_job = _active_inbound_provision_job(locked_domain)
            if active_job is not None:
                return locked_domain, active_job, False
            raise ValidationError("This domain is already using direct routing.")
        if not can_switch_domain_to_direct(locked_domain):
            raise ValidationError(
                "Routing can only be changed before domain ownership or delivery is verified."
            )

        locked_domain.inbound_setup_generation += 1
        locked_domain.setup_mode = Domain.SetupMode.DIRECT_MX
        locked_domain.status = Domain.Status.PROVISIONING
        locked_domain.ownership_verified = False
        locked_domain.inbound_ready = False
        locked_domain.outbound_ready = False
        locked_domain.outbound_status = Domain.OutboundStatus.DISABLED
        locked_domain.ses_identity_status = ""
        locked_domain.ses_identity_origin = ""
        locked_domain.verified_at = None
        locked_domain.last_checked_at = None
        locked_domain.error_code = ""
        locked_domain.error_message = ""
        locked_domain.outbound_error_code = ""
        locked_domain.outbound_error_message = ""
        locked_domain.claim_expires_at = timezone.now() + timedelta(
            hours=settings.DOMAIN_CLAIM_TTL_HOURS
        )
        locked_domain.save(
            update_fields=(
                "inbound_setup_generation",
                "setup_mode",
                "status",
                "ownership_verified",
                "inbound_ready",
                "outbound_ready",
                "outbound_status",
                "ses_identity_status",
                "ses_identity_origin",
                "verified_at",
                "last_checked_at",
                "error_code",
                "error_message",
                "outbound_error_code",
                "outbound_error_message",
                "claim_expires_at",
                "updated_at",
            )
        )
        locked_domain.inbound_routes.filter(is_active=True).update(is_active=False)
        local_part = f"route-{secrets.token_urlsafe(24).lower()}"
        InboundRoute.objects.create(
            domain=locked_domain,
            setup_generation=locked_domain.inbound_setup_generation,
            kind=InboundRoute.Kind.DIRECT_DOMAIN,
            local_part=local_part,
            address=f"{local_part}@{settings.INBOUND_SERVICE_DOMAIN}",
        )
        job = enqueue_job(
            kind="provision_domain",
            idempotency_key=(
                f"provision-domain:{locked_domain.id}:generation:"
                f"{locked_domain.inbound_setup_generation}"
            ),
            payload={
                "domain_id": str(locked_domain.id),
                "setup_generation": locked_domain.inbound_setup_generation,
                "setup_mode": locked_domain.setup_mode,
            },
            domain=locked_domain,
        )
        return locked_domain, job, True


def request_outbound_provisioning(domain: Domain) -> tuple[Domain, DurableJob, bool]:
    """Enable or retry sending without changing the receiving lifecycle."""

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        generation = locked_domain.updated_at.strftime("%Y%m%d%H%M%S%f")
        if (
            locked_domain.status
            in {
                Domain.Status.PROVISIONING,
                Domain.Status.ERROR,
                Domain.Status.DISABLED,
            }
            or not locked_domain.inbound_ready
        ):
            raise ValidationError(
                "Verify receiving with a real test email before enabling sending for this domain."
            )
        if locked_domain.outbound_status == Domain.OutboundStatus.PROVISIONING:
            active_job = (
                DurableJob.objects.filter(
                    kind="provision_outbound",
                    payload__domain_id=str(locked_domain.id),
                    status__in=[
                        DurableJob.Status.PENDING,
                        DurableJob.Status.LEASED,
                        DurableJob.Status.RETRY,
                    ],
                )
                .order_by("created_at")
                .first()
            )
            if active_job is not None:
                return locked_domain, active_job, False
        elif locked_domain.outbound_status not in {
            Domain.OutboundStatus.DISABLED,
            Domain.OutboundStatus.ERROR,
        }:
            raise ValidationError("Sending is already enabled for this domain.")

        locked_domain.outbound_status = Domain.OutboundStatus.PROVISIONING
        locked_domain.outbound_ready = False
        locked_domain.outbound_error_code = ""
        locked_domain.outbound_error_message = ""
        locked_domain.save(
            update_fields=(
                "outbound_status",
                "outbound_ready",
                "outbound_error_code",
                "outbound_error_message",
                "updated_at",
            )
        )
        job = enqueue_job(
            kind="provision_outbound",
            idempotency_key=f"provision-outbound:{locked_domain.id}:{generation}",
            payload={"domain_id": str(locked_domain.id)},
            domain=locked_domain,
        )
        return locked_domain, job, True


def schedule_work(now=None) -> int:
    now = now or timezone.now()
    count = 0
    expire_unverified_claims()
    recover_stale_submissions(now=now)
    # Repair the small commit/enqueue crash window in domain creation. Existing
    # active jobs are returned unchanged, so this scan is idempotent.
    for domain in Domain.objects.filter(status=Domain.Status.PROVISIONING):
        _, _, started = retry_domain_provisioning(domain)
        count += int(started)
    for domain in Domain.objects.filter(
        outbound_status=Domain.OutboundStatus.PROVISIONING,
        inbound_ready=True,
        status__in=[
            Domain.Status.PENDING_DNS,
            Domain.Status.PENDING_TEST,
            Domain.Status.READY,
            Domain.Status.DEGRADED,
        ],
    ):
        _, _, started = request_outbound_provisioning(domain)
        count += int(started)
    paused_owner_ids = set(
        OutboundControl.objects.filter(is_paused=True).values_list("user_id", flat=True)
    )
    for outbound in OutboundMessage.objects.filter(
        status=OutboundMessage.Status.QUEUED
    ).select_related("domain"):
        job = enqueue_job(
            kind="send_outbound",
            idempotency_key=f"outbound:{outbound.id}",
            payload={"outbound_id": str(outbound.id)},
            domain=outbound.domain,
        )
        if outbound.domain.owner_id not in paused_owner_ids and job.status in {
            DurableJob.Status.COMPLETE,
            DurableJob.Status.FAILED,
        }:
            DurableJob.objects.filter(id=job.id).update(
                status=DurableJob.Status.PENDING,
                due_at=now,
                leased_until=None,
                last_error_code="",
                updated_at=now,
            )
        count += 1
    for notification in Notification.objects.filter(
        channel=Notification.Channel.EMAIL, status=Notification.Status.PENDING
    ):
        enqueue_job(
            kind="send_notification",
            idempotency_key=f"notification:{notification.id}",
            payload={"notification_id": str(notification.id)},
            domain=notification.domain,
        )
        count += 1
    return count


def _handle(job: DurableJob) -> None:
    if job.kind == "classify_message":
        message = Message.objects.select_related("domain__owner").get(id=job.payload["message_id"])
        if not for_user(message.domain.owner).ai:
            return
        classification = classify_message(message)
        if classification is None:
            raise RuntimeError("Classification remains unavailable.")
    elif job.kind == "generate_report":
        domain = Domain.objects.select_related("owner").get(id=job.payload["domain_id"])
        if not for_user(domain.owner).ai:
            return
        scheduled_for = datetime.fromisoformat(job.payload["scheduled_for"])
        report = generate_report(
            domain=domain,
            kind=job.payload["kind"],
            now=scheduled_for,
        )
        schedule = domain.report_schedule
        if schedule.last_review_at is None or report.period_end > schedule.last_review_at:
            schedule.last_review_at = report.period_end
        if report.kind == Report.Kind.DAILY:
            schedule.last_daily_report_local_date = report.period_end.astimezone(
                domain_zone(domain)
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
        provision_inbound(
            Domain.objects.get(id=job.payload["domain_id"]),
            expected_generation=_inbound_job_generation(job),
            expected_setup_mode=job.payload.get("setup_mode"),
        )
    elif job.kind == "provision_outbound":
        provision_outbound_identity(Domain.objects.get(id=job.payload["domain_id"]))
    elif job.kind == "provision_routing_transition":
        transition = InboundRoutingTransition.objects.get(id=job.payload["transition_id"])
        if transition.generation == int(job.payload["generation"]):
            provision_routing_transition(transition)
    elif job.kind == "complete_routing_transition":
        transition = InboundRoutingTransition.objects.get(id=job.payload["transition_id"])
        if transition.generation == int(job.payload["generation"]):
            complete_expired_routing_transition(transition)
            reconcile_receipt_rule()
    elif job.kind == "dns_check":
        from django.core.management import call_command

        call_command("check_domain_drift")
    elif job.kind == "reconcile_receipt_rule":
        reconcile_receipt_rule()
    else:
        raise ValueError(f"Unsupported job kind: {job.kind}")


def _surface_job_failure(job: DurableJob, *, terminal: bool) -> None:
    if job.kind not in {
        "provision_domain",
        "provision_outbound",
        "provision_routing_transition",
    }:
        return
    if job.kind == "provision_routing_transition":
        next_status = (
            InboundRoutingTransition.Status.FAILED
            if terminal
            else InboundRoutingTransition.Status.PREPARING
        )
        InboundRoutingTransition.objects.filter(
            id=job.payload.get("transition_id"),
            generation=int(job.payload.get("generation", 0)),
            status__in={
                InboundRoutingTransition.Status.PREPARING,
                InboundRoutingTransition.Status.WAITING_DNS,
                InboundRoutingTransition.Status.FAILED,
            },
        ).update(
            status=next_status,
            error_code=(
                "routing_transition_provision_failed"
                if terminal
                else "routing_transition_provision_retry"
            ),
            error_message=(
                "The target receiving route could not be prepared after repeated attempts."
                if terminal
                else "The target receiving route is temporarily unavailable and will retry."
            ),
            updated_at=timezone.now(),
        )
        return
    domain = (
        Domain.objects.filter(id=job.payload.get("domain_id"))
        .exclude(status=Domain.Status.DISABLED)
        .first()
    )
    if domain is None:
        return
    if job.kind == "provision_domain":
        expected_generation = _inbound_job_generation(job)
        expected_setup_mode = job.payload.get("setup_mode")
        if domain.inbound_setup_generation != expected_generation or (
            expected_setup_mode and domain.setup_mode != expected_setup_mode
        ):
            return
    if job.kind == "provision_outbound":
        domain.outbound_status = (
            Domain.OutboundStatus.ERROR if terminal else Domain.OutboundStatus.PROVISIONING
        )
        domain.outbound_ready = False
        domain.outbound_error_code = (
            "outbound_provision_failed" if terminal else "outbound_provision_retry"
        )
        domain.outbound_error_message = (
            "Sending identity provisioning failed after repeated attempts. "
            "Contact support to retry."
            if terminal
            else "Sending identity provisioning is temporarily unavailable and will retry "
            "automatically."
        )
        domain.save(
            update_fields=(
                "outbound_status",
                "outbound_ready",
                "outbound_error_code",
                "outbound_error_message",
                "updated_at",
            )
        )
        if terminal:
            AuditEvent.objects.create(
                domain=domain,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type="domain.outbound_provision_failed",
                object_type="Domain",
                object_id=domain.id,
                request_id=f"job:{job.id}",
                metadata={"attempts": job.attempts},
            )
        return
    status = Domain.Status.ERROR if terminal else Domain.Status.PROVISIONING
    error_code = "domain_provision_failed" if terminal else "domain_provision_retry"
    error_message = (
        "SES identity provisioning failed after repeated attempts. Contact support to retry."
        if terminal
        else "SES identity provisioning is temporarily unavailable and will retry automatically."
    )
    updated = Domain.objects.filter(
        id=domain.id,
        inbound_setup_generation=expected_generation,
        status=Domain.Status.PROVISIONING,
    )
    if expected_setup_mode:
        updated = updated.filter(setup_mode=expected_setup_mode)
    if not updated.update(
        status=status,
        error_code=error_code,
        error_message=error_message,
        updated_at=timezone.now(),
    ):
        return
    if terminal:
        AuditEvent.objects.create(
            domain=domain,
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
            if job.attempts >= job.max_attempts:
                next_status = DurableJob.Status.FAILED
                count_key = "failed"
                terminal = True
            else:
                next_status = DurableJob.Status.RETRY
                count_key = "retry"
                terminal = False
            retry_at = (
                job.due_at
                if terminal
                else timezone.now() + timedelta(minutes=min(60, 2**job.attempts))
            )
            settled = DurableJob.objects.filter(
                id=job.id,
                status=DurableJob.Status.LEASED,
                attempts=job.attempts,
            ).update(
                status=next_status,
                leased_until=None,
                due_at=retry_at,
                last_error_code="job_handler_failed",
                updated_at=timezone.now(),
            )
            if not settled:
                # A newer worker reclaimed this lease. Its attempt owns job
                # finalization and any user-visible failure state.
                continue
            counts[count_key] += 1
            job.refresh_from_db()
            _surface_job_failure(job, terminal=terminal)
        else:
            settled = DurableJob.objects.filter(
                id=job.id,
                status=DurableJob.Status.LEASED,
                attempts=job.attempts,
            ).update(
                status=DurableJob.Status.COMPLETE,
                leased_until=None,
                last_error_code="",
                updated_at=timezone.now(),
            )
            if settled:
                counts["complete"] += 1
    return counts
