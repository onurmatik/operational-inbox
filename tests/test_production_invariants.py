from __future__ import annotations

import json
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from django.core.mail import EmailMessage as DjangoEmailMessage
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.email_backend import SESEmailBackend
from inbox.models import (
    APIToken,
    Attachment,
    Classification,
    Domain,
    DomainDNSRecord,
    DomainTest,
    DurableJob,
    IngressEvent,
    Message,
    MessageRecipient,
    Notification,
    OutboundMessage,
    ReplyDraft,
    ReplyDraftRevision,
    Report,
    ReportItem,
)
from inbox.services.domains import (
    apply_domain_readiness,
    create_domain,
    create_domain_test,
    expire_unverified_claims,
)
from inbox.services.drafts import approve_exact_revision, resend_outbound, revise_draft
from inbox.services.ingestion import (
    PermanentIngressError,
    process_sqs_body,
    quarantine_invalid_sqs_body,
)
from inbox.services.jobs import schedule_work
from inbox.services.outbound import recover_stale_submissions, submit_outbound
from inbox.services.retention import purge_retention
from tests.test_ingestion import BUCKET, DELIVERY_TOPIC, FakeS3, create_route, sns_body


def _ready_outbound(owner, organization, project, conversation, inbound_message):
    Domain.objects.create(
        organization=organization,
        project=project,
        hostname="example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        ownership_verified=True,
        inbound_ready=True,
        outbound_ready=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    MessageRecipient.objects.create(
        organization=organization,
        message=inbound_message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address="privacy@example.org",
        is_routing_recipient=True,
    )
    draft = ReplyDraft.objects.create(
        organization=organization,
        project=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = revise_draft(
        draft=draft,
        owner=owner,
        subject="Re: Privacy request",
        body_text="We received your request.",
    )
    outbound = approve_exact_revision(
        draft=draft,
        revision_id=revision.id,
        content_hash=revision.content_hash,
        owner=owner,
    )
    return draft, revision, outbound


def test_generic_ses_email_does_not_enter_outbound_delivery_pipeline():
    ses = Mock()
    backend = SESEmailBackend(ses_client=ses)
    message = DjangoEmailMessage(
        subject="Verify your email",
        body="Verification link",
        from_email="notifications@operationalinbox.com",
        to=["owner@example.com"],
    )
    assert backend.send_messages([message]) == 1
    kwargs = ses.send_raw_email.call_args.kwargs
    assert "ConfigurationSetName" not in kwargs
    assert "Tags" not in kwargs


def test_deploy_contract_uses_private_ssh_and_fixed_production_mail_backend():
    source = (Path(__file__).parents[1] / ".deploy" / "fabfile.py").read_text()
    assert 'REPO_URL = f"git@github.com:' in source
    assert "forward_agent=True" in source
    assert 'git_prefix = f"GIT_SSH_COMMAND={quote(GIT_SSH_COMMAND)}"' in source
    assert "env=git_environment" not in source
    assert "safe.directory=" in source
    assert "DJANGO_EMAIL_BACKEND=ses" in source
    assert '"DJANGO_EMAIL_BACKEND",' not in source


@pytest.mark.django_db
def test_expired_claim_releases_hostname_and_disables_routes(monkeypatch, organization, project):
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    expired = create_domain(
        organization=organization,
        project=project,
        hostname="reclaim.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    Domain.objects.filter(id=expired.id).update(
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert expire_unverified_claims() == 1
    expired.refresh_from_db()
    assert expired.status == Domain.Status.DISABLED
    assert not expired.inbound_routes.filter(is_active=True).exists()
    replacement = create_domain(
        organization=organization,
        project=project,
        hostname="reclaim.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    assert replacement.id != expired.id


@pytest.mark.django_db
def test_domain_readiness_is_derived_separately(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="ready.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ownership = DomainDNSRecord.objects.create(
        organization=organization,
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name="_amazonses.ready.example",
        value="proof",
        status=DomainDNSRecord.Status.VALID,
    )
    mx = DomainDNSRecord.objects.create(
        organization=organization,
        domain=domain,
        purpose=DomainDNSRecord.Purpose.MX,
        record_type="MX",
        name="ready.example",
        value="inbound-smtp.us-east-1.amazonaws.com",
        priority=10,
        status=DomainDNSRecord.Status.VALID,
    )
    DomainTest.objects.create(
        organization=organization,
        domain=domain,
        token_hash="a" * 64,
        status=DomainTest.Status.RECEIVED,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    assert apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()
    assert domain.ownership_verified
    assert domain.inbound_ready and domain.outbound_ready
    assert domain.status == Domain.Status.READY

    mx.status = DomainDNSRecord.Status.INVALID
    mx.save(update_fields=("status", "updated_at"))
    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    degraded_domain = Domain.objects.get(pk=domain.pk)
    assert degraded_domain.status == Domain.Status.DEGRADED
    assert not degraded_domain.inbound_ready and degraded_domain.outbound_ready
    assert ownership.status == DomainDNSRecord.Status.VALID


@pytest.mark.django_db
@pytest.mark.parametrize("setup_mode", Domain.SetupMode.values)
def test_delivery_test_targets_the_customer_path(organization, project, setup_mode):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname=f"{setup_mode.casefold().replace('_', '-')}.example",
        setup_mode=setup_mode,
        status=Domain.Status.PENDING_TEST,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    test, address = create_domain_test(domain)
    assert address.startswith("test-") and address.endswith(f"@{domain.hostname}")
    assert len(test.token_hash) == 64


@pytest.mark.django_db
def test_crash_recovery_never_retries_and_edit_revokes_queued_send(
    owner, organization, project, conversation, inbound_message
):
    draft, _, outbound = _ready_outbound(
        owner, organization, project, conversation, inbound_message
    )
    OutboundMessage.objects.filter(id=outbound.id).update(
        status=OutboundMessage.Status.SUBMITTING,
        updated_at=timezone.now() - timedelta(minutes=11),
    )
    assert recover_stale_submissions() == 1
    outbound.refresh_from_db()
    assert outbound.status == OutboundMessage.Status.UNKNOWN

    resend = resend_outbound(outbound, owner=owner)
    revise_draft(
        draft=draft,
        owner=owner,
        subject="Re: Privacy request",
        body_text="A newer owner-edited response.",
    )
    ses = Mock()
    result = submit_outbound(resend, ses_client=ses)
    assert result.status == OutboundMessage.Status.FAILED
    assert result.error_code == "send_authorization_revoked"
    ses.send_raw_email.assert_not_called()


def _threaded_email(message_id: str, *, in_reply_to: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "sender@example.net"
    message["To"] = "privacy@example.org"
    message["Subject"] = "Re: Privacy request" if in_reply_to else "Privacy request"
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    message.set_content("Following up on the request.")
    return message.as_bytes()


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:inbound",
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_new_inbound_message_marks_existing_draft_stale(owner, organization, project):
    route_address = "route-stale@inbound.example"
    create_route(organization, project, route_address)
    first_id = "<thread-first@example.net>"
    s3 = FakeS3(_threaded_email(first_id))
    assert process_sqs_body(sns_body([route_address]), s3_client=s3)
    message = Message.objects.get(project=project)
    draft = ReplyDraft.objects.create(
        organization=organization,
        project=project,
        conversation=message.conversation,
        context_message=message,
    )
    revise_draft(
        draft=draft,
        owner=owner,
        subject="Re: Privacy request",
        body_text="Draft before the follow-up.",
    )
    s3.objects[(BUCKET, "ingress/ses-2")] = _threaded_email(
        "<thread-second@example.net>", in_reply_to=first_id
    )
    assert process_sqs_body(
        sns_body(
            [route_address],
            sns_id="sns-stale-2",
            ses_id="ses-2",
            object_key="ingress/ses-2",
        ),
        s3_client=s3,
    )
    draft.refresh_from_db()
    assert draft.is_stale


@pytest.mark.django_db
@override_settings(
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:123456789012:inbound",
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_delivery_tag_reconciles_unknown_send_without_state_regression(
    owner, organization, project, conversation, inbound_message
):
    _, _, outbound = _ready_outbound(owner, organization, project, conversation, inbound_message)
    outbound.status = OutboundMessage.Status.UNKNOWN
    outbound.save(update_fields=("status", "updated_at"))

    def delivery_body(event_type: str, sns_id: str) -> str:
        inner = {
            "eventType": event_type,
            "mail": {
                "messageId": "ses-correlated",
                "timestamp": "2026-07-31T12:00:00Z",
                "tags": {"outbound_id": [str(outbound.id)]},
            },
        }
        return json.dumps(
            {
                "Type": "Notification",
                "TopicArn": DELIVERY_TOPIC,
                "MessageId": sns_id,
                "Message": json.dumps(inner),
            }
        )

    assert process_sqs_body(delivery_body("Delivery", "sns-delivered"))
    outbound.refresh_from_db()
    assert outbound.status == OutboundMessage.Status.DELIVERED
    assert outbound.provider_message_id == "ses-correlated"
    assert process_sqs_body(delivery_body("Reject", "sns-late-reject"))
    outbound.refresh_from_db()
    assert outbound.status == OutboundMessage.Status.DELIVERED


@pytest.mark.django_db
def test_malformed_queue_payload_leaves_an_inspectable_quarantine_record():
    event = quarantine_invalid_sqs_body(
        "not-json",
        PermanentIngressError("invalid_sqs_body", "SQS body is not valid JSON."),
    )
    assert event.status == IngressEvent.Status.QUARANTINED
    assert event.error_code == "invalid_sqs_body"
    assert IngressEvent.objects.count() == 1


@pytest.mark.django_db
def test_retention_redacts_normalized_personal_content(
    owner, organization, project, conversation, inbound_message
):
    policy = organization.retention_policy
    policy.raw_message_days = 1
    policy.attachment_days = 1
    policy.normalized_content_days = 1
    policy.save()
    old = timezone.now() - timedelta(days=2)
    inbound_message.received_at = old
    inbound_message.save(update_fields=("received_at", "updated_at"))
    classification = Classification.objects.create(
        organization=organization,
        message=inbound_message,
        source=Classification.Source.OWNER,
        category=Classification.Category.ACTIONABLE,
        topic="Sensitive customer request",
        summary="Personal summary",
        recommended_action="Personal action",
    )
    attachment = Attachment.objects.create(
        organization=organization,
        message=inbound_message,
        display_name="customer-name.txt",
        content_type="text/plain",
        size=10,
        sha256="d" * 64,
        s3_key="organizations/private.txt",
        scan_status=Attachment.ScanStatus.CLEAN,
        purge_at=timezone.now() + timedelta(days=90),
    )
    notification = Notification.objects.create(
        organization=organization,
        project=project,
        conversation=conversation,
        channel=Notification.Channel.IN_APP,
        kind="sensitive",
        dedupe_key="retention-sensitive",
        title="Customer name",
        body="Sensitive notification body",
    )
    Notification.objects.filter(id=notification.id).update(created_at=old)
    report = Report.objects.create(
        organization=organization,
        kind=Report.Kind.DAILY,
        schedule_key="old-retention-report",
        period_start=old - timedelta(days=1),
        period_end=old,
        status=Report.Status.READY,
        title="Sensitive report",
        content="Sensitive report content",
    )
    report_item = ReportItem.objects.create(
        organization=organization,
        report=report,
        conversation=conversation,
        classification=classification,
        rank=1,
        summary="Sensitive item",
    )
    _, revision, outbound = _ready_outbound(
        owner, organization, project, conversation, inbound_message
    )
    ReplyDraftRevision.objects.filter(id=revision.id).update(created_at=old)
    OutboundMessage.objects.filter(id=outbound.id).update(created_at=old)

    s3 = Mock()
    purge_retention(s3_client=s3, now=timezone.now())
    inbound_message.refresh_from_db()
    classification.refresh_from_db()
    attachment.refresh_from_db()
    notification.refresh_from_db()
    report.refresh_from_db()
    report_item.refresh_from_db()
    revision.refresh_from_db()
    outbound.refresh_from_db()
    assert inbound_message.from_address == "redacted@invalid.local"
    assert inbound_message.subject == ""
    assert not inbound_message.recipients.exists()
    assert classification.topic == classification.summary == ""
    assert attachment.display_name == "expired attachment" and attachment.sha256 == ""
    assert notification.body == "" and report.content == "" and report_item.summary == ""
    assert revision.subject == revision.body_text == ""
    assert outbound.to_address == "redacted@invalid.local" and outbound.body_text == ""


@pytest.mark.django_db
@override_settings(SIGNUP_RATE_LIMIT=1, SIGNUP_RATE_WINDOW_SECONDS=3600)
def test_signup_rate_limit_is_durable_and_returns_retry_after(client):
    payload = {"email": "not-an-email"}
    assert client.post(reverse("signup"), payload, REMOTE_ADDR="203.0.113.10").status_code == 400
    limited = client.post(reverse("signup"), payload, REMOTE_ADDR="203.0.113.10")
    assert limited.status_code == 429
    assert limited["Retry-After"] == "3600"


@pytest.mark.django_db
def test_disabled_tenant_invalidates_bearer_token(client, owner, organization):
    _, raw = APIToken.issue(
        organization=organization,
        owner=owner,
        name="Read",
        scopes=[APIToken.Scope.READ],
    )
    organization.is_active = False
    organization.save(update_fields=("is_active", "updated_at"))
    response = client.get(
        f"/api/v1/organizations/{organization.id}/projects",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_hourly_scheduler_catches_up_missed_runs(organization):
    now = datetime(2026, 7, 31, 12, 5, tzinfo=ZoneInfo("UTC"))
    schedule = organization.report_schedule
    schedule.review_frequency = schedule.Frequency.HOURLY
    schedule.last_review_at = now - timedelta(hours=3, minutes=5)
    schedule.save(update_fields=("review_frequency", "last_review_at", "updated_at"))
    schedule_work(now=now)
    jobs = list(
        DurableJob.objects.filter(kind="generate_report").order_by("payload__scheduled_for")
    )
    assert len(jobs) == 3
    assert jobs[-1].payload["scheduled_for"].startswith("2026-07-31T12:00:00")
