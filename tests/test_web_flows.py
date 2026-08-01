from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Classification,
    Conversation,
    Domain,
    Notification,
    Organization,
    Project,
    Report,
)


@pytest.mark.django_db
def test_authenticated_application_pages_render(
    client, owner, organization, project, conversation, inbound_message
):
    client.force_login(owner)
    client.session["organization_id"] = str(organization.id)
    client.session.save()
    Classification.objects.create(
        organization=organization,
        message=inbound_message,
        source=Classification.Source.OWNER,
        category=Classification.Category.ACTIONABLE,
        urgency=Classification.Urgency.HIGH,
        summary="Review this message.",
        recommended_action="Respond after verifying the request.",
    )
    Domain.objects.create(
        organization=organization,
        project=project,
        hostname="example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        inbound_ready=True,
        outbound_ready=True,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    Notification.objects.create(
        organization=organization,
        project=project,
        conversation=conversation,
        channel=Notification.Channel.IN_APP,
        kind="important",
        dedupe_key="web-test",
        title="Review required",
        body="An actionable message arrived.",
    )
    Report.objects.create(
        organization=organization,
        kind=Report.Kind.DAILY,
        schedule_key="2026-07-31:daily",
        period_start=timezone.now() - timedelta(days=1),
        period_end=timezone.now(),
        status=Report.Status.READY,
        title="Daily review",
        content="One actionable item.",
    )
    AuditEvent.objects.create(
        organization=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type="test.rendered",
        object_type="Conversation",
        object_id=conversation.id,
        request_id="web-test",
    )
    routes = [
        reverse("dashboard"),
        reverse("inbox"),
        reverse("conversation_detail", args=[conversation.id]),
        reverse("domains"),
        reverse("domain_create"),
        reverse("projects"),
        reverse("reports"),
        reverse("notifications"),
        reverse("schedules_settings"),
        reverse("api_tokens"),
        reverse("audit"),
    ]
    for url in routes:
        response = client.get(url)
        assert response.status_code == 200, url
        assert b"Operational Inbox" in response.content


@pytest.mark.django_db
def test_conversation_state_and_api_token_web_actions(
    client, owner, organization, project, conversation
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    state = client.post(
        reverse("conversation_status", args=[conversation.id]), {"status": "RESOLVED"}
    )
    assert state.status_code == 302
    conversation.refresh_from_db()
    assert conversation.status == "RESOLVED"
    create = client.post(
        reverse("api_tokens"),
        {"name": "Web automation", "scopes": ["read", "write"]},
    )
    assert create.status_code == 302
    token = APIToken.objects.get(name="Web automation")
    reveal = client.get(reverse("api_tokens"))
    assert reveal.status_code == 200
    assert b"shown again" in reveal.content
    assert b'id="new-token"' not in client.get(reverse("api_tokens")).content
    revoke = client.post(reverse("api_token_revoke", args=[token.id]))
    assert revoke.status_code == 302
    token.refresh_from_db()
    assert token.revoked_at is not None


@pytest.mark.django_db
def test_attachment_web_locked_and_expired_responses(client, owner, organization, inbound_message):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    attachment = Attachment.objects.create(
        organization=organization,
        message=inbound_message,
        display_name="malware.bin",
        content_type="application/octet-stream",
        size=4,
        sha256="c" * 64,
        s3_key="tenant/malware.bin",
        scan_status=Attachment.ScanStatus.QUARANTINED,
        purge_at=timezone.now() + timedelta(days=1),
    )
    locked = client.get(reverse("attachment_download", args=[attachment.id]))
    assert locked.status_code == 423
    attachment.scan_status = Attachment.ScanStatus.CLEAN
    attachment.purge_at = timezone.now() - timedelta(seconds=1)
    attachment.save(update_fields=("scan_status", "purge_at", "updated_at"))
    expired = client.get(reverse("attachment_download", args=[attachment.id]))
    assert expired.status_code == 410


@pytest.mark.django_db
def test_owner_can_switch_organizations_and_project_selection_is_cleared(
    client, owner, organization, project
):
    second = Organization.objects.create(owner=owner, name="Second Operations", slug="second")
    Project.objects.create(organization=second, name="Second Project", slug="second")
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session["project_id"] = str(project.id)
    session.save()
    response = client.post(
        reverse("organization_switch"),
        {"organization_id": str(second.id), "next": reverse("dashboard")},
    )
    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session["organization_id"] == str(second.id)
    assert "project_id" not in client.session


@pytest.mark.django_db
def test_complete_inbox_has_filter_preserving_pagination(
    client, owner, organization, project, conversation
):
    now = timezone.now()
    for index in range(50):
        Conversation.objects.create(
            organization=organization,
            project=project,
            subject=f"Paged conversation {index:02d}",
            normalized_subject=f"paged conversation {index:02d}",
            first_message_at=now - timedelta(minutes=index + 1),
            last_message_at=now - timedelta(minutes=index + 1),
        )
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    first = client.get(reverse("inbox"), {"state": "OPEN"})
    second = client.get(reverse("inbox"), {"state": "OPEN", "page": 2})
    assert first.status_code == 200 and len(first.context["conversations"]) == 50
    assert second.status_code == 200 and len(second.context["conversations"]) == 1
    assert b"state=OPEN&amp;page=2" in first.content


@pytest.mark.django_db
def test_quarantined_inbox_preview_never_leaks_body(
    client, owner, organization, conversation, inbound_message
):
    inbound_message.is_quarantined = True
    inbound_message.text_body = "DO-NOT-LEAK-QUARANTINED-CONTENT"
    inbound_message.save(update_fields=("is_quarantined", "text_body", "updated_at"))
    conversation.status = Conversation.Status.QUARANTINED
    conversation.save(update_fields=("status", "updated_at"))
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    response = client.get(reverse("inbox"))
    assert b"DO-NOT-LEAK-QUARANTINED-CONTENT" not in response.content
    assert b"Content locked by the malware quarantine" in response.content
