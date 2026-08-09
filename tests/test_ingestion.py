from __future__ import annotations

import hashlib
import io
import json
from datetime import timedelta
from email.message import EmailMessage

import pytest
from django.test import override_settings
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DomainTest,
    InboundRoute,
    IngressEvent,
    Message,
    User,
)
from inbox.services.domains import build_inbound_dns_instructions, ensure_domain_test
from inbox.services.ingestion import process_sqs_body
from tests.test_mime_threading import raw_email

INBOUND_TOPIC = "arn:aws:sns:us-east-1:123456789012:inbound"
DELIVERY_TOPIC = "arn:aws:sns:us-east-1:123456789012:delivery"
BUCKET = "operational-inbox-test"


class FakeS3:
    def __init__(self, raw: bytes):
        self.objects = {(BUCKET, "ingress/ses-1"): raw}
        self.copies: list[str] = []

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def copy_object(self, *, Bucket, Key, CopySource, **kwargs):
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.copies.append(Key)
        return {}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body
        return {}


def sns_body(
    recipients: list[str],
    *,
    sns_id: str = "sns-1",
    ses_id: str = "ses-1",
    virus: str = "PASS",
    object_key: str = "ingress/ses-1",
) -> str:
    inner = {
        "notificationType": "Received",
        "mail": {
            "messageId": ses_id,
            "timestamp": "2026-07-31T12:00:00Z",
        },
        "receipt": {
            "recipients": recipients,
            "spamVerdict": {"status": "PASS"},
            "virusVerdict": {"status": virus},
            "dkimVerdict": {"status": "PASS"},
            "spfVerdict": {"status": "PASS"},
            "dmarcVerdict": {"status": "PASS"},
            "action": {"type": "S3", "bucketName": BUCKET, "objectKey": object_key},
        },
    }
    return json.dumps(
        {
            "Type": "Notification",
            "TopicArn": INBOUND_TOPIC,
            "MessageId": sns_id,
            "Message": json.dumps(inner),
        }
    )


def create_route(organization, project, address: str) -> Domain:
    domain = project
    domain.setup_mode = Domain.SetupMode.PROVIDER_FORWARD
    domain.status = Domain.Status.PENDING_TEST
    domain.ownership_verified = True
    domain.save(update_fields=("setup_mode", "status", "ownership_verified", "updated_at"))
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part=address.split("@", 1)[0],
        address=address,
    )
    return domain


def raw_email_to(
    address: str,
    *,
    from_address: str = "arbitrary-sender@elsewhere.example",
) -> bytes:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = address
    message["Subject"] = "Receiving verification"
    message["Message-ID"] = "<receiving-verification@example.net>"
    message.set_content("Verify the configured inbound path.")
    return message.as_bytes()


def raw_follow_up(address: str) -> bytes:
    message = EmailMessage()
    message["From"] = "Sender <sender@example.net>"
    message["To"] = address
    message["Subject"] = "Re: Hello"
    message["Message-ID"] = "<follow-up@example.net>"
    message["In-Reply-To"] = "<new@example.net>"
    message.set_content("Following up with new information.")
    return message.as_bytes()


def provider_forward_challenge(project, *, alias: str) -> tuple[Domain, InboundRoute, DomainTest]:
    domain = create_route(project, project, alias)
    domain.inbound_ready = False
    domain.save(update_fields=("inbound_ready", "updated_at"))
    build_inbound_dns_instructions(domain, ownership_token="provider-forward-proof")
    domain.dns_records.filter(is_required=True).update(status=DomainDNSRecord.Status.VALID)
    test, address, created = ensure_domain_test(domain)
    assert created is True
    assert test.address == address
    return domain, domain.inbound_routes.get(address=alias), test


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_duplicate_sns_and_ses_messages_are_idempotent(organization, project):
    address = "route-abc@inbound.example"
    create_route(organization, project, address)
    s3 = FakeS3(raw_email())
    assert process_sqs_body(sns_body([address]), s3_client=s3)
    assert process_sqs_body(sns_body([address]), s3_client=s3)
    assert Message.objects.filter(domain=project).count() == 1
    assert IngressEvent.objects.count() == 1

    # Different SNS IDs can redeliver the same SES message without creating a duplicate.
    assert process_sqs_body(sns_body([address], sns_id="sns-2"), s3_client=s3)
    assert Message.objects.filter(domain=project).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("removed_field", ["archived_at", "trashed_at"])
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_new_inbound_restores_removed_conversation_and_preserves_star(
    organization,
    project,
    removed_field,
):
    address = f"route-{removed_field}@inbound.example"
    create_route(organization, project, address)
    process_sqs_body(sns_body([address]), s3_client=FakeS3(raw_email()))
    conversation = Message.objects.get().conversation
    now = timezone.now()
    conversation.status = conversation.Status.RESOLVED
    conversation.resolved_at = now
    conversation.starred_at = now
    conversation.work_started_at = now
    setattr(conversation, removed_field, now)
    conversation.save(
        update_fields=(
            "status",
            "resolved_at",
            "starred_at",
            "work_started_at",
            removed_field,
            "updated_at",
        )
    )
    Message.objects.filter(conversation=conversation).update(viewed_at=now)

    process_sqs_body(
        sns_body([address], sns_id="sns-follow-up", ses_id="ses-follow-up"),
        s3_client=FakeS3(raw_follow_up(address)),
    )

    conversation.refresh_from_db()
    assert conversation.status == conversation.Status.OPEN
    assert conversation.resolved_at is None
    assert conversation.archived_at is None
    assert conversation.trashed_at is None
    assert conversation.starred_at == now
    assert conversation.work_started_at == now
    assert (
        Message.objects.filter(
            conversation=conversation,
            direction=Message.Direction.INBOUND,
            viewed_at__isnull=True,
        ).count()
        == 1
    )


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_one_ses_message_routes_to_independent_tenant_copies(organization, project):
    address_one = "route-one@inbound.example"
    create_route(organization, project, address_one)
    owner_two = User.objects.create_user(email="two@example.com", password="Password-123456")
    project_two = Domain.objects.create(
        owner=owner_two,
        hostname="second.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_TEST,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    address_two = "route-two@second.example"
    create_route(project_two, project_two, address_two)
    s3 = FakeS3(raw_email(attachment=True))
    assert process_sqs_body(sns_body([address_one, address_two]), s3_client=s3)
    messages = list(Message.objects.order_by("domain_id"))
    assert len(messages) == 2
    assert messages[0].raw_s3_key != messages[1].raw_s3_key
    assert all("domains/" in item.raw_s3_key for item in messages)
    assert len(s3.copies) == 2


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_virus_verdict_quarantines_message_and_attachments(organization, project):
    address = "route-virus@inbound.example"
    create_route(organization, project, address)
    s3 = FakeS3(raw_email(attachment=True))
    process_sqs_body(sns_body([address], virus="FAIL"), s3_client=s3)
    message = Message.objects.get()
    assert message.is_quarantined
    assert message.conversation.status == message.conversation.Status.QUARANTINED
    assert message.attachments.get().scan_status == "QUARANTINED"


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_malformed_mime_is_permanently_quarantined(organization, project):
    address = "route-bad@inbound.example"
    create_route(organization, project, address)
    oversized = b"x" * (40 * 1024 * 1024 + 1)
    s3 = FakeS3(oversized)
    assert process_sqs_body(sns_body([address]), s3_client=s3)
    event = IngressEvent.objects.get()
    assert event.status == IngressEvent.Status.QUARANTINED
    assert event.error_code == "malformed_mime"
    assert not Message.objects.exists()


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_unroutable_envelope_does_not_use_mime_to(organization, project):
    s3 = FakeS3(raw_email())
    assert process_sqs_body(sns_body(["unknown@inbound.example"]), s3_client=s3)
    assert not Message.objects.exists()
    assert IngressEvent.objects.get().error_code == "unroutable_recipient"


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_forwarding_alias_is_not_accepted_after_domain_switches_to_direct(organization, project):
    address = "route-old-forwarding@inbound.example"
    domain = create_route(organization, project, address)
    domain.setup_mode = Domain.SetupMode.DIRECT_MX
    domain.save(update_fields=("setup_mode", "updated_at"))

    s3 = FakeS3(raw_email())
    assert process_sqs_body(sns_body([address]), s3_client=s3)

    assert not Message.objects.exists()
    assert IngressEvent.objects.get().error_code == "unroutable_recipient"


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_provider_forward_challenge_marks_receiving_ready_through_real_sqs_and_mime(project):
    domain, forwarding_route, test = provider_forward_challenge(
        project,
        alias="route-verification@inbound.example",
    )
    sender = "someone-unrelated@outside.example"
    s3 = FakeS3(raw_email_to(str(test.address), from_address=sender))

    assert process_sqs_body(sns_body([forwarding_route.address]), s3_client=s3)

    domain.refresh_from_db()
    test.refresh_from_db()
    event = IngressEvent.objects.get()
    message = Message.objects.get(domain=domain)
    assert event.status == IngressEvent.Status.PROCESSED
    assert message.from_address == sender
    assert test.status == DomainTest.Status.RECEIVED
    assert test.received_message_id == message.id
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert (
        AuditEvent.objects.filter(
            domain=domain,
            event_type="domain.test_received",
            object_id=test.id,
        ).count()
        == 1
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario",
    ["normal_address", "token_hash_only", "expired", "wrong_generation"],
)
@override_settings(
    AWS_INGRESS_BUCKET=BUCKET,
    AWS_INBOUND_TOPIC_ARN=INBOUND_TOPIC,
    AWS_DELIVERY_TOPIC_ARN=DELIVERY_TOPIC,
)
def test_provider_forward_challenge_requires_the_exact_current_address(project, scenario):
    domain, forwarding_route, test = provider_forward_challenge(
        project,
        alias="route-exact-address@inbound.example",
    )
    mime_recipient = str(test.address)
    if scenario == "normal_address":
        mime_recipient = f"ordinary@{domain.hostname}"
    elif scenario == "token_hash_only":
        similar_raw = "looks-like-the-current-token"
        mime_recipient = f"test-{similar_raw}@{domain.hostname}"
        test.token_hash = hashlib.sha256(similar_raw.encode()).hexdigest()
        test.save(update_fields=("token_hash", "updated_at"))
    elif scenario == "expired":
        test.expires_at = timezone.now() - timedelta(seconds=1)
        test.save(update_fields=("expires_at", "updated_at"))
    elif scenario == "wrong_generation":
        test.setup_generation = domain.inbound_setup_generation + 1
        test.save(update_fields=("setup_generation", "updated_at"))

    s3 = FakeS3(raw_email_to(mime_recipient))
    assert process_sqs_body(sns_body([forwarding_route.address]), s3_client=s3)

    domain.refresh_from_db()
    test.refresh_from_db()
    assert IngressEvent.objects.get().status == IngressEvent.Status.PROCESSED
    assert Message.objects.filter(domain=domain).count() == 1
    assert test.status == DomainTest.Status.PENDING
    assert test.received_message_id is None
    assert domain.status == Domain.Status.PENDING_TEST
    assert not domain.inbound_ready
    assert not AuditEvent.objects.filter(
        domain=domain,
        event_type="domain.test_received",
    ).exists()
