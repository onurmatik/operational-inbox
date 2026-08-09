from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Classification,
    Domain,
    DomainDNSRecord,
    DurableJob,
    InboundRoute,
    Notification,
)
from inbox.services.ingestion import process_sqs_body
from inbox.services.jobs import (
    enqueue_job,
    request_outbound_provisioning,
    retry_domain_provisioning,
    run_due_jobs,
    schedule_work,
    switch_domain_to_direct,
)
from inbox.services.notifications import (
    create_security_notifications,
    send_pending_email_notification,
)

DELIVERY_TOPIC = "arn:aws:sns:us-east-1:123456789012:delivery"


@pytest.mark.django_db
def test_durable_job_retries_and_reclaims_expired_lease(monkeypatch, organization, inbound_message):
    job = enqueue_job(
        kind="classify_message",
        idempotency_key=f"classify:{inbound_message.id}",
        payload={"message_id": str(inbound_message.id)},
        domain=organization,
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
        domain=organization,
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
        owner=project.owner,
        hostname="mail.example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    job = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={"domain_id": str(domain.id)},
        domain=organization,
    )
    job.max_attempts = 1
    job.save(update_fields=("max_attempts", "updated_at"))
    monkeypatch.setattr(
        "inbox.services.jobs.provision_inbound",
        lambda candidate: (_ for _ in ()).throw(RuntimeError("secret AWS detail")),
    )
    counts = run_due_jobs(limit=1)
    domain.refresh_from_db()
    assert counts["failed"] == 1
    assert domain.status == Domain.Status.ERROR
    assert domain.error_code == "domain_provision_failed"
    assert "secret AWS detail" not in domain.error_message
    assert "Operational Inbox could not finish preparing this domain" in domain.public_error_message
    assert "SES" not in domain.public_error_message
    assert AuditEvent.objects.filter(event_type="domain.provision_failed").exists()


@pytest.mark.django_db
def test_terminal_outbound_failure_preserves_ready_inbound(monkeypatch, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="send-failure.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.READY,
        inbound_ready=True,
        ownership_verified=True,
        outbound_status=Domain.OutboundStatus.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    route = InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-send-failure",
        address="route-send-failure@inbound.example.net",
    )
    job = enqueue_job(
        kind="provision_outbound",
        idempotency_key=f"provision-outbound:{domain.id}",
        payload={"domain_id": str(domain.id)},
        domain=domain,
    )
    job.max_attempts = 1
    job.save(update_fields=("max_attempts", "updated_at"))
    monkeypatch.setattr(
        "inbox.services.jobs.provision_outbound_identity",
        lambda candidate: (_ for _ in ()).throw(RuntimeError("secret AWS detail")),
    )

    counts = run_due_jobs(limit=1)

    domain.refresh_from_db()
    route.refresh_from_db()
    assert counts["failed"] == 1
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert route.is_active
    assert domain.error_code == ""
    assert domain.outbound_status == Domain.OutboundStatus.ERROR
    assert not domain.outbound_ready
    assert domain.outbound_error_code == "outbound_provision_failed"
    assert "secret AWS detail" not in domain.outbound_error_message
    assert "SES" not in domain.public_outbound_error_message
    assert AuditEvent.objects.filter(event_type="domain.outbound_provision_failed").exists()


@pytest.mark.django_db
def test_outbound_enable_is_idempotent_and_does_not_change_inbound(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="enable-send.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.READY,
        inbound_ready=True,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )

    first_domain, first_job, started = request_outbound_provisioning(domain)
    second_domain, second_job, repeated_started = request_outbound_provisioning(first_domain)

    assert started is True
    assert repeated_started is False
    assert second_job.id == first_job.id
    assert second_domain.status == Domain.Status.READY
    assert second_domain.inbound_ready
    assert second_domain.outbound_status == Domain.OutboundStatus.PROVISIONING
    assert not second_domain.outbound_ready


@pytest.mark.django_db
def test_recoverable_domain_retry_creates_one_new_active_job(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
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
        domain=organization,
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
def test_unverified_provider_setup_switches_to_a_fenced_direct_generation(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="reconnect.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )
    old_route = InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-old",
        address="route-old@inbound.example.net",
    )
    claim = DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name="_operational-inbox-claim.reconnect.example",
        value="fresh-current-claim",
    )

    switched, job, started = switch_domain_to_direct(domain)
    repeated, repeated_job, repeated_started = switch_domain_to_direct(switched)

    domain.refresh_from_db()
    old_route.refresh_from_db()
    claim.refresh_from_db()
    assert started is True
    assert repeated_started is False
    assert repeated_job.id == job.id
    assert repeated.inbound_setup_generation == 2
    assert domain.setup_mode == Domain.SetupMode.DIRECT_MX
    assert domain.status == Domain.Status.PROVISIONING
    assert domain.inbound_setup_generation == 2
    assert claim.value == "fresh-current-claim"
    assert not old_route.is_active
    assert (
        domain.inbound_routes.filter(
            kind=InboundRoute.Kind.DIRECT_DOMAIN,
            is_active=True,
        ).count()
        == 1
    )
    assert job.payload == {
        "domain_id": str(domain.id),
        "setup_generation": 2,
        "setup_mode": Domain.SetupMode.DIRECT_MX,
    }


@pytest.mark.django_db
def test_stale_provider_job_completes_without_writing_provider_instructions(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="stale-provider-noop.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-stale-noop",
        address="route-stale-noop@inbound.example.net",
    )
    stale = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={
            "domain_id": str(domain.id),
            "setup_generation": 1,
            "setup_mode": Domain.SetupMode.PROVIDER_FORWARD,
        },
        domain=domain,
    )
    switched, current_job, _ = switch_domain_to_direct(domain)
    current_job.due_at = timezone.now() + timedelta(minutes=10)
    current_job.save(update_fields=("due_at", "updated_at"))

    counts = run_due_jobs(limit=1)

    switched.refresh_from_db()
    stale.refresh_from_db()
    assert counts["complete"] == 1
    assert stale.status == DurableJob.Status.COMPLETE
    assert switched.setup_mode == Domain.SetupMode.DIRECT_MX
    assert switched.inbound_setup_generation == 2
    assert switched.status == Domain.Status.PROVISIONING
    assert not switched.dns_records.exists()


@pytest.mark.django_db
def test_stale_provider_job_cannot_overwrite_or_fail_a_new_direct_generation(monkeypatch, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="stale-provider.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-stale-provider",
        address="route-stale-provider@inbound.example.net",
    )
    stale = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={
            "domain_id": str(domain.id),
            "setup_generation": 1,
            "setup_mode": Domain.SetupMode.PROVIDER_FORWARD,
        },
        domain=domain,
    )
    stale.max_attempts = 1
    stale.save(update_fields=("max_attempts", "updated_at"))
    switched, current_job, _ = switch_domain_to_direct(domain)
    current_job.due_at = timezone.now() + timedelta(minutes=10)
    current_job.save(update_fields=("due_at", "updated_at"))

    def stale_failure(*args, **kwargs):
        raise RuntimeError("old provider attempt failed")

    monkeypatch.setattr("inbox.services.jobs.provision_inbound", stale_failure)
    counts = run_due_jobs(limit=1)

    switched.refresh_from_db()
    stale.refresh_from_db()
    assert counts["failed"] == 1
    assert stale.status == DurableJob.Status.FAILED
    assert switched.setup_mode == Domain.SetupMode.DIRECT_MX
    assert switched.inbound_setup_generation == 2
    assert switched.status == Domain.Status.PROVISIONING
    assert switched.error_code == ""


@pytest.mark.django_db
def test_reclaimed_provision_failure_cannot_regress_completed_domain_state(monkeypatch, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="reclaimed-provision.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )
    job = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}:generation:1",
        payload={
            "domain_id": str(domain.id),
            "setup_generation": 1,
            "setup_mode": Domain.SetupMode.DIRECT_MX,
        },
        domain=domain,
    )
    job.max_attempts = 1
    job.save(update_fields=("max_attempts", "updated_at"))

    def complete_domain_then_raise(candidate, **kwargs):
        Domain.objects.filter(id=candidate.id).update(status=Domain.Status.PENDING_DNS)
        raise RuntimeError("a reclaimed worker failed after another worker completed")

    monkeypatch.setattr("inbox.services.jobs.provision_inbound", complete_domain_then_raise)

    counts = run_due_jobs(limit=1)

    domain.refresh_from_db()
    assert counts["failed"] == 1
    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.error_code == ""
    assert not AuditEvent.objects.filter(
        domain=domain,
        event_type="domain.provision_failed",
    ).exists()


@pytest.mark.django_db
def test_expired_worker_lease_cannot_finalize_over_a_newer_attempt(monkeypatch, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="lease-fence.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )
    job = enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}:lease-fence",
        payload={
            "domain_id": str(domain.id),
            "setup_generation": 1,
            "setup_mode": Domain.SetupMode.DIRECT_MX,
        },
        domain=domain,
    )

    def newer_worker_reclaims_lease(candidate, **kwargs):
        DurableJob.objects.filter(id=job.id).update(
            status=DurableJob.Status.RETRY,
            attempts=2,
            leased_until=None,
            due_at=timezone.now() + timedelta(minutes=10),
        )
        return candidate

    monkeypatch.setattr("inbox.services.jobs.provision_inbound", newer_worker_reclaims_lease)

    counts = run_due_jobs(limit=1)

    job.refresh_from_db()
    assert counts["complete"] == 0
    assert job.status == DurableJob.Status.RETRY
    assert job.attempts == 2


@pytest.mark.django_db
def test_verified_provider_setup_cannot_switch_routing_mode(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="verified-provider.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_TEST,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(hours=1),
    )

    with pytest.raises(ValidationError, match="before domain ownership"):
        switch_domain_to_direct(domain)


@pytest.mark.django_db
def test_scheduler_repairs_provisioning_domain_without_an_active_job(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
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
    inbound_message.is_suspicious = True
    inbound_message.save(update_fields=("is_suspicious", "updated_at"))
    first = create_security_notifications(inbound_message)
    second = create_security_notifications(inbound_message)
    assert Notification.objects.count() == 1
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
