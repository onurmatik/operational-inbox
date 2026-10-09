from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.test import override_settings
from django.utils import timezone

from inbox.models import (
    Attachment,
    AuditEvent,
    DurableJob,
    EmailVerificationToken,
    IngressEvent,
    SignupAttempt,
)
from inbox.services.attachments import (
    AttachmentGoneError,
    AttachmentLockedError,
    authorized_attachment_url,
)
from inbox.services.retention import purge_retention


@pytest.mark.django_db
@override_settings(AWS_INGRESS_BUCKET="bucket")
def test_attachment_authorization_clean_locked_and_expired(organization, inbound_message):
    s3 = Mock()
    s3.generate_presigned_url.return_value = "https://signed.example/download"
    clean = Attachment.objects.create(
        domain=organization,
        message=inbound_message,
        display_name="report.pdf",
        content_type="application/pdf",
        size=100,
        sha256="a" * 64,
        s3_key="tenant/report.pdf",
        scan_status=Attachment.ScanStatus.CLEAN,
        purge_at=timezone.now() + timedelta(days=1),
    )
    authorized = authorized_attachment_url(attachment=clean, domain=organization, s3_client=s3)
    assert authorized.expires_in == 300
    assert s3.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300
    clean.scan_status = Attachment.ScanStatus.QUARANTINED
    with pytest.raises(AttachmentLockedError):
        authorized_attachment_url(attachment=clean, domain=organization, s3_client=s3)
    clean.scan_status = Attachment.ScanStatus.CLEAN
    clean.purge_at = timezone.now() - timedelta(seconds=1)
    with pytest.raises(AttachmentGoneError):
        authorized_attachment_url(attachment=clean, domain=organization, s3_client=s3)


@pytest.mark.django_db
@override_settings(AWS_INGRESS_BUCKET="bucket")
def test_retention_purges_content_but_keeps_message_tombstone(organization, inbound_message, owner):
    policy = organization.retention_policy
    policy.raw_message_days = 1
    policy.attachment_days = 1
    policy.normalized_content_days = 1
    policy.audit_metadata_days = 1
    policy.delivery_metadata_days = 1
    policy.save()
    old = timezone.now() - timedelta(days=2)
    inbound_message.received_at = old
    inbound_message.raw_s3_key = "tenant/raw.eml"
    inbound_message.save(update_fields=("received_at", "raw_s3_key", "updated_at"))
    attachment = Attachment.objects.create(
        domain=organization,
        message=inbound_message,
        display_name="old.txt",
        content_type="text/plain",
        size=3,
        sha256="b" * 64,
        s3_key="tenant/old.txt",
        scan_status=Attachment.ScanStatus.CLEAN,
        purge_at=old,
    )
    AuditEvent.objects.create(
        domain=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type="old.event",
        object_type="Message",
        request_id="old",
    )
    AuditEvent.all_objects.filter(event_type="old.event").update(created_at=old)
    s3 = Mock()
    counts = purge_retention(s3_client=s3, now=timezone.now())
    inbound_message.refresh_from_db()
    attachment.refresh_from_db()
    assert inbound_message.text_body == ""
    assert inbound_message.raw_s3_key == ""
    assert inbound_message.normalized_purged_at is not None
    assert MessageExists(inbound_message.id)
    assert attachment.scan_status == Attachment.ScanStatus.EXPIRED
    assert counts["raw"] == 1 and counts["attachments"] == 1


def _ingress_event(*, suffix, domain, created_at):
    event = IngressEvent.objects.create(
        sns_message_id=f"sns-{suffix}",
        ses_message_id=f"ses-{suffix}",
        source_topic_arn="arn:aws:sns:us-east-1:123456789012:inbound",
        source_bucket="private-ingress",
        source_key=f"ingress/{suffix}",
        payload_digest=suffix.rjust(64, "0")[-64:],
        status=IngressEvent.Status.PROCESSED,
        processed_at=created_at,
    )
    IngressEvent.objects.filter(id=event.id).update(
        created_at=created_at,
        updated_at=created_at,
    )
    return event


@pytest.mark.django_db
@override_settings(AWS_INGRESS_BUCKET="bucket")
def test_retention_redacts_ingress_location_before_expiring_event_metadata(organization):
    now = timezone.now()
    policy = organization.retention_policy
    policy.raw_message_days = 1
    policy.audit_metadata_days = 3
    policy.save(update_fields=("raw_message_days", "audit_metadata_days", "updated_at"))

    recent = _ingress_event(
        suffix="recent",
        domain=organization,
        created_at=now - timedelta(hours=12),
    )
    raw_expired = _ingress_event(
        suffix="raw-expired",
        domain=organization,
        created_at=now - timedelta(days=2),
    )
    metadata_expired = _ingress_event(
        suffix="metadata-expired",
        domain=organization,
        created_at=now - timedelta(days=4),
    )
    global_recent = _ingress_event(
        suffix="global-recent",
        domain=None,
        created_at=now - timedelta(days=2),
    )
    global_raw_expired = _ingress_event(
        suffix="global-raw-expired",
        domain=None,
        created_at=now - timedelta(days=91),
    )
    global_metadata_expired = _ingress_event(
        suffix="global-metadata-expired",
        domain=None,
        created_at=now - timedelta(days=731),
    )

    counts = purge_retention(s3_client=Mock(), now=now)

    recent.refresh_from_db()
    raw_expired.refresh_from_db()
    global_recent.refresh_from_db()
    global_raw_expired.refresh_from_db()
    assert recent.source_bucket == "private-ingress"
    assert raw_expired.source_bucket == "private-ingress"
    assert global_recent.source_bucket == "private-ingress"
    assert global_raw_expired.source_bucket == "" and global_raw_expired.source_key == ""
    assert IngressEvent.objects.filter(id=metadata_expired.id).exists()
    assert not IngressEvent.objects.filter(id=global_metadata_expired.id).exists()
    assert counts["ingress_raw"] == 1
    assert counts["ingress_metadata"] == 1


def _durable_job(*, suffix, domain, status, updated_at):
    job = DurableJob.objects.create(
        domain=domain,
        kind="retention-test",
        idempotency_key=f"retention-test:{suffix}",
        status=status,
        payload={"private": suffix},
        due_at=updated_at,
    )
    DurableJob.objects.filter(id=job.id).update(
        created_at=updated_at,
        updated_at=updated_at,
    )
    return job


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET="bucket",
    SIGNUP_RATE_WINDOW_SECONDS=3600,
    VERIFICATION_RESEND_RATE_WINDOW_SECONDS=7200,
)
def test_retention_purges_ephemeral_auth_records_and_only_terminal_old_jobs(organization, owner):
    now = timezone.now()
    policy = organization.retention_policy
    policy.audit_metadata_days = 1
    policy.save(update_fields=("audit_metadata_days", "updated_at"))
    old = now - timedelta(days=2)

    complete = _durable_job(
        suffix="complete",
        domain=organization,
        status=DurableJob.Status.COMPLETE,
        updated_at=old,
    )
    failed = _durable_job(
        suffix="failed",
        domain=organization,
        status=DurableJob.Status.FAILED,
        updated_at=old,
    )
    active_jobs = [
        _durable_job(
            suffix=status.casefold(),
            domain=organization,
            status=status,
            updated_at=old,
        )
        for status in [
            DurableJob.Status.PENDING,
            DurableJob.Status.LEASED,
            DurableJob.Status.RETRY,
        ]
    ]
    recent_complete = _durable_job(
        suffix="recent-complete",
        domain=organization,
        status=DurableJob.Status.COMPLETE,
        updated_at=now - timedelta(hours=12),
    )
    global_safe = _durable_job(
        suffix="global-safe",
        domain=None,
        status=DurableJob.Status.COMPLETE,
        updated_at=old,
    )
    global_expired = _durable_job(
        suffix="global-expired",
        domain=None,
        status=DurableJob.Status.COMPLETE,
        updated_at=now - timedelta(days=731),
    )

    old_attempt = SignupAttempt.objects.create(
        fingerprint_hash="a" * 64,
        email_hash="b" * 64,
        accepted=False,
    )
    recent_attempt = SignupAttempt.objects.create(
        fingerprint_hash="c" * 64,
        email_hash="d" * 64,
        accepted=True,
    )
    SignupAttempt.objects.filter(id=old_attempt.id).update(created_at=now - timedelta(hours=2))
    SignupAttempt.objects.filter(id=recent_attempt.id).update(
        created_at=now - timedelta(minutes=30)
    )
    old_resend = SignupAttempt.objects.create(
        kind=SignupAttempt.Kind.VERIFICATION_RESEND,
        fingerprint_hash="2" * 64,
        email_hash="3" * 64,
        accepted=False,
    )
    recent_resend = SignupAttempt.objects.create(
        kind=SignupAttempt.Kind.VERIFICATION_RESEND,
        fingerprint_hash="4" * 64,
        email_hash="5" * 64,
        accepted=True,
    )
    SignupAttempt.objects.filter(id=old_resend.id).update(created_at=now - timedelta(hours=3))
    SignupAttempt.objects.filter(id=recent_resend.id).update(created_at=now - timedelta(minutes=90))

    expired_token = EmailVerificationToken.objects.create(
        user=owner,
        token_hash="e" * 64,
        expires_at=now - timedelta(seconds=1),
    )
    used_token = EmailVerificationToken.objects.create(
        user=owner,
        token_hash="f" * 64,
        expires_at=now + timedelta(days=1),
        used_at=now,
    )
    valid_token = EmailVerificationToken.objects.create(
        user=owner,
        token_hash="1" * 64,
        expires_at=now + timedelta(days=1),
    )

    counts = purge_retention(s3_client=Mock(), now=now)

    assert not DurableJob.objects.filter(id__in=[complete.id, failed.id]).exists()
    assert not DurableJob.objects.filter(id=global_expired.id).exists()
    assert (
        DurableJob.objects.filter(
            id__in=[*(job.id for job in active_jobs), recent_complete.id, global_safe.id]
        ).count()
        == 5
    )
    assert not SignupAttempt.objects.filter(id=old_attempt.id).exists()
    assert SignupAttempt.objects.filter(id=recent_attempt.id).exists()
    assert not SignupAttempt.objects.filter(id=old_resend.id).exists()
    assert SignupAttempt.objects.filter(id=recent_resend.id).exists()
    assert not EmailVerificationToken.objects.filter(
        id__in=[expired_token.id, used_token.id]
    ).exists()
    assert EmailVerificationToken.objects.filter(id=valid_token.id).exists()
    assert counts["terminal_jobs"] == 3
    assert counts["signup_attempts"] == 2
    assert counts["verification_tokens"] == 2


def MessageExists(message_id):
    from inbox.models import Message

    return Message.objects.filter(id=message_id).exists()
