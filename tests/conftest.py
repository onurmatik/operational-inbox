from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from inbox.models import (
    Conversation,
    Message,
    Organization,
    Project,
    ReportSchedule,
    RetentionPolicy,
    User,
)


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(
        email="owner@example.com",
        password="Correct-Horse-Battery-123",
        email_verified_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def organization(owner: User) -> Organization:
    organization = Organization.objects.create(
        owner=owner, name="Example Operations", slug="example", timezone="Europe/Istanbul"
    )
    ReportSchedule.objects.create(organization=organization)
    RetentionPolicy.objects.create(organization=organization)
    return organization


@pytest.fixture
def project(organization: Organization) -> Project:
    return Project.objects.create(
        organization=organization, name="Primary Operations", slug="primary"
    )


@pytest.fixture
def conversation(project: Project) -> Conversation:
    now = timezone.now()
    return Conversation.objects.create(
        organization=project.organization,
        project=project,
        subject="Privacy request",
        normalized_subject="privacy request",
        first_message_at=now,
        last_message_at=now,
        last_inbound_at=now,
    )


@pytest.fixture
def inbound_message(conversation: Conversation) -> Message:
    now = timezone.now()
    return Message.objects.create(
        organization=conversation.organization,
        project=conversation.project,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        provider_message_id="ses-message-1",
        rfc_message_id="<message-1@example.net>",
        from_address="sender@example.net",
        reply_to_address="reply@example.net",
        subject="Privacy request",
        text_body="Please confirm receipt of my privacy request.",
        received_at=now - timedelta(minutes=2),
        spam_verdict=Message.Verdict.PASS,
        virus_verdict=Message.Verdict.PASS,
        dkim_verdict=Message.Verdict.PASS,
        spf_verdict=Message.Verdict.PASS,
        dmarc_verdict=Message.Verdict.PASS,
    )
