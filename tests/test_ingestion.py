from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from inbox.models import Domain, InboundRoute, IngressEvent, Message, Organization, Project, User
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
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname=address.rsplit("@", 1)[1],
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_TEST,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    InboundRoute.objects.create(
        organization=organization,
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part=address.split("@", 1)[0],
        address=address,
    )
    return domain


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
    assert Message.objects.filter(project=project).count() == 1
    assert IngressEvent.objects.count() == 1

    # Different SNS IDs can redeliver the same SES message without creating a duplicate.
    assert process_sqs_body(sns_body([address], sns_id="sns-2"), s3_client=s3)
    assert Message.objects.filter(project=project).count() == 1


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
    org_two = Organization.objects.create(owner=owner_two, name="Two", slug="two")
    project_two = Project.objects.create(organization=org_two, name="Two", slug="two")
    address_two = "route-two@second.example"
    create_route(org_two, project_two, address_two)
    s3 = FakeS3(raw_email(attachment=True))
    assert process_sqs_body(sns_body([address_one, address_two]), s3_client=s3)
    messages = list(Message.objects.order_by("organization_id"))
    assert len(messages) == 2
    assert messages[0].raw_s3_key != messages[1].raw_s3_key
    assert all("organizations/" in item.raw_s3_key for item in messages)
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
