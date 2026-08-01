from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from inbox.models import (
    APIToken,
    AuditEvent,
    Conversation,
    DraftApproval,
    Organization,
    Project,
    ReplyDraft,
    ReplyDraftRevision,
)


@pytest.mark.django_db
def test_project_scoped_model_rejects_cross_tenant_project(owner, organization, project):
    other = Organization.objects.create(owner=owner, name="Other", slug="other")
    other_project = Project.objects.create(organization=other, name="Other", slug="other")
    now = timezone.now()
    conversation = Conversation(
        organization=organization,
        project=other_project,
        subject="Mismatch",
        first_message_at=now,
        last_message_at=now,
    )
    with pytest.raises(ValidationError, match="same organization"):
        conversation.full_clean()


@pytest.mark.django_db
def test_api_token_is_hashed_scoped_and_shown_once(owner, organization):
    token, raw = APIToken.issue(
        organization=organization,
        owner=owner,
        name="Automation",
        scopes=[APIToken.Scope.READ, APIToken.Scope.WRITE],
    )
    assert raw.startswith("oi_")
    assert raw not in token.token_hash
    assert token.matches(raw)
    assert token.has_scope("read")
    assert not token.has_scope("approve_send")
    token.expires_at = timezone.now() - timedelta(seconds=1)
    assert not token.is_active


@pytest.mark.django_db
def test_audit_event_is_append_only(owner, organization):
    event = AuditEvent.objects.create(
        organization=organization,
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
        organization=organization,
        project=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    first = ReplyDraftRevision(
        organization=organization,
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
        organization=organization,
        revision=first,
        approved_by=owner,
        content_hash="not-the-hash",
    )
    with pytest.raises(ValidationError):
        approval.full_clean()


@pytest.mark.django_db
def test_header_injection_is_rejected(organization, project, conversation, inbound_message, owner):
    draft = ReplyDraft.objects.create(
        organization=organization,
        project=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = ReplyDraftRevision(
        organization=organization,
        draft=draft,
        number=1,
        subject="Hello\r\nBcc: victim@example.com",
        body_text="Body",
        author=owner,
    )
    with pytest.raises(ValidationError, match="line breaks"):
        revision.full_clean()
