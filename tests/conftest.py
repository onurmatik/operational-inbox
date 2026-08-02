from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from inbox.models import (
    BillingProfile,
    Conversation,
    Domain,
    Message,
    ReportSchedule,
    RetentionPolicy,
    User,
)


@pytest.fixture
def owner(db) -> User:
    user = User.objects.create_user(
        email="owner@example.com",
        password="Correct-Horse-Battery-123",
        email_verified_at=timezone.now(),
        is_active=True,
    )
    BillingProfile.objects.create(
        user=user,
        subscription_status=BillingProfile.SubscriptionStatus.ACTIVE,
        subscription_plan="pro",
    )
    return user


@pytest.fixture
def domain(owner: User) -> Domain:
    domain = Domain.objects.create(
        owner=owner,
        hostname="example.com",
        timezone="Europe/Istanbul",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        ownership_verified=True,
        inbound_ready=True,
        outbound_ready=True,
        outbound_status=Domain.OutboundStatus.READY,
        claim_expires_at=timezone.now() + timedelta(days=3),
    )
    ReportSchedule.objects.create(domain=domain)
    RetentionPolicy.objects.create(domain=domain)
    return domain


@pytest.fixture
def organization(domain: Domain) -> Domain:
    """Compatibility fixture name while individual behavior tests migrate to domain terminology."""
    return domain


@pytest.fixture
def project(domain: Domain) -> Domain:
    """Compatibility fixture name while individual behavior tests migrate to domain terminology."""
    return domain


@pytest.fixture
def conversation(domain: Domain) -> Conversation:
    now = timezone.now()
    return Conversation.objects.create(
        domain=domain,
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
        domain=conversation.domain,
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
