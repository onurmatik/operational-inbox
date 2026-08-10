from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils import timezone

from inbox.models import (
    APIToken,
    AuditEvent,
    Conversation,
    Domain,
    DomainDNSRecord,
    DraftApproval,
    ReplyDraft,
    ReplyDraftRevision,
)


@pytest.mark.django_db
def test_domain_scoped_models_validate_related_domain(owner, organization, project):
    other = Domain.objects.create(
        owner=owner,
        hostname="other.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    now = timezone.now()
    conversation = Conversation(
        domain=other,
        subject="Mismatch",
        first_message_at=now,
        last_message_at=now,
    )
    conversation.full_clean()
    assert conversation.domain == other


@pytest.mark.django_db
def test_api_token_is_hashed_global_and_rotated(owner):
    token, raw = APIToken.issue(owner=owner)
    assert raw.startswith("oi_")
    assert raw not in token.token_hash
    assert token.matches(raw)
    replacement, replacement_raw = APIToken.issue(owner=owner)
    token.refresh_from_db()
    assert token.revoked_at is not None
    assert not token.is_active
    assert replacement.matches(replacement_raw)
    assert APIToken.objects.filter(owner=owner, revoked_at__isnull=True).get() == replacement
    token.expires_at = timezone.now() - timedelta(seconds=1)
    assert not token.is_active


@pytest.mark.django_db
def test_audit_event_is_append_only(owner, organization):
    event = AuditEvent.objects.create(
        domain=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type="test.created",
        object_type="Test",
        request_id="request-1",
    )
    event.event_type = "test.modified"
    with pytest.raises(ValidationError, match="append-only"):
        event.save()
    with pytest.raises(ValidationError, match="append-only"):
        AuditEvent.objects.filter(id=event.id).delete()


@pytest.mark.django_db
def test_revision_is_immutable_and_approval_requires_current_exact_revision(
    owner, organization, project, conversation, inbound_message
):
    draft = ReplyDraft.objects.create(
        domain=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    first = ReplyDraftRevision(
        domain=organization,
        draft=draft,
        number=1,
        subject="Re: Privacy request",
        body_text="We received your request.",
        author=owner,
    )
    first.full_clean()
    first.save()
    draft.current_revision = first
    draft.save(update_fields=("current_revision", "updated_at"))
    first.body_text = "Changed in place"
    with pytest.raises(ValidationError, match="immutable"):
        first.save()
    approval = DraftApproval(
        domain=organization,
        revision=first,
        approved_by=owner,
        content_hash="not-the-hash",
    )
    with pytest.raises(ValidationError):
        approval.full_clean()


@pytest.mark.django_db
def test_header_injection_is_rejected(organization, project, conversation, inbound_message, owner):
    draft = ReplyDraft.objects.create(
        domain=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = ReplyDraftRevision(
        domain=organization,
        draft=draft,
        number=1,
        subject="Hello\r\nBcc: victim@example.com",
        body_text="Body",
        author=owner,
    )
    with pytest.raises(ValidationError, match="line breaks"):
        revision.full_clean()


@pytest.mark.django_db
def test_outbound_split_backfill_does_not_treat_legacy_dkim_as_user_intent(project):
    pending = Domain.objects.create(
        owner=project.owner,
        hostname="legacy-pending.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ready = Domain.objects.create(
        owner=project.owner,
        hostname="legacy-ready.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        outbound_ready=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    for domain in (pending, ready):
        DomainDNSRecord.objects.create(
            domain=domain,
            purpose=DomainDNSRecord.Purpose.DKIM,
            record_type="CNAME",
            name=f"one._domainkey.{domain.hostname}",
            value="one.dkim.amazonses.com",
            is_required=False,
        )
    DomainDNSRecord.objects.create(
        domain=pending,
        purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
        record_type="TXT",
        name=f"_amazonses.{pending.hostname}",
        value="legacy-proof",
        is_required=False,
    )
    migration = importlib.import_module("inbox.migrations.0002_split_outbound_provisioning")

    migration.backfill_outbound_status(apps, None)

    pending.refresh_from_db()
    ready.refresh_from_db()
    assert pending.outbound_status == Domain.OutboundStatus.DISABLED
    assert not pending.dns_records.filter(
        purpose__in=[
            DomainDNSRecord.Purpose.DKIM,
            DomainDNSRecord.Purpose.SES_VERIFICATION,
        ]
    ).exists()
    assert ready.outbound_status == Domain.OutboundStatus.READY
    assert ready.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exists()
