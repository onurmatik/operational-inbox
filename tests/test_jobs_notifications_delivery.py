from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from inbox.models import AuditEvent, Classification, Domain, DurableJob, Notification
from inbox.services.ingestion import process_sqs_body
from inbox.services.jobs import (
    enqueue_job,
    retry_domain_provisioning,
    run_due_jobs,
    schedule_work,
)
from inbox.services.notifications import (
    create_classification_notifications,
    send_pending_email_notification,
)

DELIVERY_TOPIC = "arn:aws:sns:us-east-1:123456789012:delivery"


@pytest.mark.django_db
def test_durable_job_retries_and_reclaims_expired_lease(monkeypatch, organization, inbound_message):
    job = enqueue_job(
        kind="classify_message",
        idempotency_key=f"classify:{inbound_message.id}",
        payload={"message_id": str(inbound_message.id)},
        organization=organization,
    )
    monkeypatch.setattr("inbox.services.jobs.classify_message", lambda message: None)
    counts = run_due_jobs(limit=1)
    job.refresh_from_db()
    assert counts["retry"] == 1 and job.status == DurableJob.Status.RETRY
    job.status = DurableJob.Status.LEASED
    job.leased_until = timezone.now() - timedelta(seconds=1)
    job.due_at = timezone.now() - timedelta(seconds=1)
    job.save()
    classification = Classification.objects.create(
        organization=organization,
        message=inbound_message,
        source=Classification.Source.OWNER,
        category=Classification.Category.INFORMATIONAL,
        urgency=Classification.Urgency.NORMAL,
    )
    monkeypatch.setattr("inbox.services.jobs.classify_message", lambda message: classification)
    reclaimed = run_due_jobs(limit=1)
    job.refresh_from_db()
    assert reclaimed["complete"] == 1 and job.status == DurableJob.Status.COMPLETE


@pytest.mark.django_db
def test_terminal_domain_provision_failure_is_actionable_and_sanitized(
    monkeypatch, organization, project
):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="mail.example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    job = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={"domain_id": str(domain.id)},
        organization=organization,
    )
    job.max_attempts = 1
    job.save(update_fields=("max_attempts", "updated_at"))
    monkeypatch.setattr(
        "inbox.services.jobs.provision_ses_identity",
        lambda candidate: (_ for _ in ()).throw(RuntimeError("secret AWS detail")),
    )
    counts = run_due_jobs(limit=1)
    domain.refresh_from_db()
    assert counts["failed"] == 1
    assert domain.status == Domain.Status.ERROR
    assert domain.error_code == "domain_provision_failed"
    assert "secret AWS detail" not in domain.error_message
    assert AuditEvent.objects.filter(event_type="domain.provision_failed").exists()


@pytest.mark.django_db
def test_recoverable_domain_retry_creates_one_new_active_job(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="retry-existing.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.ERROR,
        error_code="ses_identity_collision",
        error_message="Legacy collision",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    completed = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={"domain_id": str(domain.id)},
        organization=organization,
    )
    completed.status = DurableJob.Status.COMPLETE
    completed.save(update_fields=("status", "updated_at"))

    retried_domain, retry_job, started = retry_domain_provisioning(domain)
    same_domain, same_job, repeated_started = retry_domain_provisioning(retried_domain)

    assert started is True
    assert repeated_started is False
    assert same_domain.status == Domain.Status.PROVISIONING
    assert same_job.id == retry_job.id
    assert retry_job.id != completed.id
    assert retry_job.status == DurableJob.Status.PENDING
    assert (
        DurableJob.objects.filter(
            kind="provision_domain",
            payload__domain_id=str(domain.id),
            status__in=[
                DurableJob.Status.PENDING,
                DurableJob.Status.LEASED,
                DurableJob.Status.RETRY,
            ],
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_scheduler_repairs_provisioning_domain_without_an_active_job(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="stranded-provisioning.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )

    schedule_work()
    first_job = DurableJob.objects.get(kind="provision_domain", payload__domain_id=str(domain.id))
    schedule_work()

    assert first_job.status == DurableJob.Status.PENDING
    assert (
        DurableJob.objects.filter(
            kind="provision_domain", payload__domain_id=str(domain.id)
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_important_notification_is_deduplicated_and_email_delivered(
    mailoutbox, organization, inbound_message
):
    classification = Classification.objects.create(
        organization=organization,
        message=inbound_message,
        source=Classification.Source.OWNER,
        category=Classification.Category.SUSPICIOUS,
        urgency=Classification.Urgency.HIGH,
        summary="Suspicious authentication failure.",
    )
    first = create_classification_notifications(classification)
    second = create_classification_notifications(classification)
    assert Notification.objects.count() == 2
    assert [item.id for item in first] == [item.id for item in second]
    email_notification = Notification.objects.get(channel=Notification.Channel.EMAIL)
    assert send_pending_email_notification(email_notification)
    assert len(mailoutbox) == 1
    assert send_pending_email_notification(email_notification)
    assert len(mailoutbox) == 1


@pytest.mark.django_db
@override_settings(
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:inbound",
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_delivery_event_retries_when_outbound_acceptance_is_not_visible():
    inner = {
        "notificationType": "Delivery",
        "mail": {"messageId": "not-visible-yet", "timestamp": "2026-07-31T12:00:00Z"},
    }
    body = json.dumps(
        {
            "Type": "Notification",
            "TopicArn": DELIVERY_TOPIC,
            "MessageId": "delivery-sns-1",
            "Message": json.dumps(inner),
        }
    )
    assert process_sqs_body(body) is False
