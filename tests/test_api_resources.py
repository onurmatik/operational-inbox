from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from inbox.models import (
    Attachment,
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DurableJob,
    Notification,
    Organization,
    Project,
    ReplyDraft,
    Report,
)
from inbox.services.drafts import revise_draft


@pytest.mark.django_db
def test_domain_retry_api_starts_one_recoverable_attempt(client, owner, organization, project):
    client.force_login(owner)
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="api-retry.example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.ERROR,
        error_code="domain_provision_failed",
        error_message="Setup failed.",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    url = f"/api/v1/organizations/{organization.id}/domains/{domain.id}/retry"

    first = client.post(url, data={}, content_type="application/json")
    second = client.post(url, data={}, content_type="application/json")

    assert first.status_code == 202
    assert first.json()["started"] is True
    assert second.status_code == 202
    assert second.json()["started"] is False
    assert second.json()["job_id"] == first.json()["job_id"]

    domain.status = Domain.Status.PENDING_DNS
    domain.save(update_fields=("status", "updated_at"))
    rejected = client.post(url, data={}, content_type="application/json")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "domain_retry_not_allowed"


@pytest.mark.django_db
def test_repeated_domain_create_returns_existing_claim(
    client, monkeypatch, owner, organization, project
):
    client.force_login(owner)
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    url = f"/api/v1/organizations/{organization.id}/domains"
    payload = {
        "project_id": str(project.id),
        "hostname": "idempotent.example.org",
        "setup_mode": "DIRECT_MX",
    }

    first = client.post(url, data=payload, content_type="application/json")
    DurableJob.objects.get(kind="provision_domain", payload__domain_id=first.json()["id"]).delete()
    second = client.post(url, data=payload, content_type="application/json")
    third = client.post(url, data=payload, content_type="application/json")
    mismatched = client.post(
        url,
        data={**payload, "setup_mode": "PROVIDER_FORWARD"},
        content_type="application/json",
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert third.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert mismatched.status_code == 409
    assert mismatched.json()["code"] == "domain_claim_conflict"
    assert Domain.objects.filter(hostname="idempotent.example.org").count() == 1
    assert (
        DurableJob.objects.filter(
            kind="provision_domain",
            payload__domain_id=first.json()["id"],
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_cross_tenant_domain_conflict_is_enumeration_safe(
    client, monkeypatch, owner, organization, project
):
    other_owner = type(owner).objects.create_user(email="other-owner@example.org")
    other_organization = Organization.objects.create(
        owner=other_owner,
        name="Other",
        slug="other",
    )
    other_project = Project.objects.create(
        organization=other_organization,
        name="Default",
        slug="default",
    )
    Domain.objects.create(
        organization=other_organization,
        project=other_project,
        hostname="claimed-elsewhere.example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )

    client.force_login(owner)
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    response = client.post(
        f"/api/v1/organizations/{organization.id}/domains",
        data={
            "project_id": str(project.id),
            "hostname": "claimed-elsewhere.example.org",
            "setup_mode": "DIRECT_MX",
        },
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "domain_claim_conflict"
    assert str(other_organization.id) not in response.content.decode()


@pytest.mark.django_db
def test_session_api_resource_surface(
    client, monkeypatch, owner, organization, project, conversation, inbound_message
):
    client.force_login(owner)
    base = f"/api/v1/organizations/{organization.id}"
    assert client.get("/api/v1/organizations").status_code == 200
    created_project = client.post(
        f"{base}/projects",
        data={"name": "Security"},
        content_type="application/json",
    )
    assert created_project.status_code == 200 or created_project.status_code == 201

    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    domain_response = client.post(
        f"{base}/domains",
        data={
            "project_id": str(project.id),
            "hostname": "inbound.example.org",
            "setup_mode": "PROVIDER_FORWARD",
        },
        content_type="application/json",
    )
    assert domain_response.status_code == 202
    domain_id = domain_response.json()["id"]
    assert client.get(f"{base}/domains/{domain_id}").status_code == 200
    premature_check = client.post(
        f"{base}/domains/{domain_id}/check", data={}, content_type="application/json"
    )
    assert premature_check.status_code == 409
    assert premature_check.json()["code"] == "dns_instructions_not_ready"
    premature_test = client.post(
        f"{base}/domains/{domain_id}/test", data={}, content_type="application/json"
    )
    assert premature_test.status_code == 409
    assert premature_test.json()["code"] == "domain_not_ready_for_test"

    domain = Domain.objects.get(id=domain_id)
    domain.status = Domain.Status.PENDING_TEST
    domain.ownership_verified = True
    domain.save(update_fields=("status", "ownership_verified", "updated_at"))
    DomainDNSRecord.objects.create(
        organization=organization,
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name="_amazonses.inbound.example.org",
        value="ownership-proof",
        status=DomainDNSRecord.Status.VALID,
    )
    first_check = client.post(
        f"{base}/domains/{domain_id}/check", data={}, content_type="application/json"
    )
    assert first_check.status_code == 202
    first_check_id = first_check.json()["job_id"]
    retry_due_at = timezone.now() + timedelta(hours=1)
    DurableJob.objects.filter(id=first_check_id).update(
        status=DurableJob.Status.RETRY,
        due_at=retry_due_at,
    )
    repeated_check = client.post(
        f"{base}/domains/{domain_id}/check", data={}, content_type="application/json"
    )
    assert repeated_check.status_code == 202
    assert repeated_check.json()["job_id"] == first_check_id
    expedited = DurableJob.objects.get(id=first_check_id)
    assert expedited.due_at < retry_due_at
    DurableJob.objects.filter(id=first_check_id).update(status=DurableJob.Status.COMPLETE)
    completed_check = client.post(
        f"{base}/domains/{domain_id}/check", data={}, content_type="application/json"
    )
    assert completed_check.status_code == 202
    assert completed_check.json()["job_id"] != first_check_id
    monkeypatch.setattr("inbox.services.receipt_rules.reconcile_receipt_rule", lambda: None)
    test_delivery = client.post(
        f"{base}/domains/{domain_id}/test", data={}, content_type="application/json"
    )
    assert test_delivery.status_code == 201
    assert test_delivery.json()["address"].endswith("@inbound.example.org")

    conversations = client.get(f"{base}/conversations")
    assert conversations.status_code == 200
    assert conversations.json()["items"][0]["id"] == str(conversation.id)
    detail = client.get(f"{base}/conversations/{conversation.id}")
    assert detail.status_code == 200 and detail.json()["messages"]
    state = client.post(
        f"{base}/conversations/{conversation.id}/state",
        data={"status": "RESOLVED"},
        content_type="application/json",
    )
    assert state.status_code == 200 and state.json()["status"] == "RESOLVED"
    override = client.post(
        f"{base}/messages/{inbound_message.id}/classification",
        data={
            "category": "ACTIONABLE",
            "urgency": "HIGH",
            "summary": "Owner review",
            "recommended_action": "Respond",
            "requires_reply": True,
        },
        content_type="application/json",
    )
    assert override.status_code == 201
    draft = ReplyDraft.objects.create(
        organization=organization,
        project=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = revise_draft(
        draft=draft,
        owner=owner,
        subject="Re: Owner review",
        body_text="Reviewable exact content.",
    )
    draft_detail = client.get(f"{base}/drafts/{draft.id}")
    assert draft_detail.status_code == 200
    assert draft_detail.json()["current_revision"] == {
        "id": str(revision.id),
        "number": 1,
        "subject": "Re: Owner review",
        "body_text": "Reviewable exact content.",
        "content_hash": revision.content_hash,
    }

    report = Report.objects.create(
        organization=organization,
        kind=Report.Kind.HOURLY,
        schedule_key="api-hour",
        period_start=timezone.now() - timedelta(hours=1),
        period_end=timezone.now(),
        status=Report.Status.READY,
        title="Hourly review",
        content="Review complete.",
    )
    notification = Notification.objects.create(
        organization=organization,
        channel=Notification.Channel.IN_APP,
        kind="api",
        dedupe_key="api-notification",
        title="API notification",
        body="Review this.",
    )
    AuditEvent.objects.create(
        organization=organization,
        actor_type=AuditEvent.ActorType.SYSTEM,
        event_type="api.test",
        object_type="Report",
        object_id=report.id,
        request_id="api-test",
    )
    assert client.get(f"{base}/reports").json()["items"]
    assert client.get(f"{base}/notifications").json()["items"]
    read = client.post(
        f"{base}/notifications/{notification.id}/read",
        data={},
        content_type="application/json",
    )
    assert read.status_code == 200
    assert client.get(f"{base}/audit").json()["items"]

    token = client.post(
        f"{base}/tokens",
        data={"name": "Full API", "scopes": ["read", "write", "approve_send"]},
        content_type="application/json",
    )
    assert token.status_code == 201
    assert token.json()["token"].startswith("oi_")
    assert AuditEvent.objects.filter(
        organization=organization,
        event_type="conversation.state_changed",
    ).exists()
    assert AuditEvent.objects.filter(
        organization=organization,
        event_type="classification.overridden",
    ).exists()
    assert AuditEvent.objects.filter(
        organization=organization,
        event_type="api_token.created",
    ).exists()


@pytest.mark.django_db
def test_api_attachment_url_contract(client, monkeypatch, owner, organization, inbound_message):
    client.force_login(owner)
    attachment = Attachment.objects.create(
        organization=organization,
        message=inbound_message,
        display_name="clean.txt",
        content_type="text/plain",
        size=5,
        sha256="d" * 64,
        s3_key="tenant/clean.txt",
        scan_status=Attachment.ScanStatus.CLEAN,
        purge_at=timezone.now() + timedelta(days=1),
    )
    s3 = Mock()
    s3.generate_presigned_url.return_value = "https://signed.example/clean"
    monkeypatch.setattr("inbox.services.attachments.boto3.client", lambda *args, **kwargs: s3)
    response = client.get(
        f"/api/v1/organizations/{organization.id}/attachments/{attachment.id}/url"
    )
    assert response.status_code == 200
    assert response.json() == {"url": "https://signed.example/clean", "expires_in": 300}


@pytest.mark.django_db
def test_api_opaque_cursor_and_invalid_cursor_contract(
    client, owner, organization, project, conversation
):
    client.force_login(owner)
    from inbox.models import Conversation

    for index in range(3):
        at = timezone.now() - timedelta(minutes=index + 1)
        Conversation.objects.create(
            organization=organization,
            project=project,
            subject=f"Conversation {index}",
            first_message_at=at,
            last_message_at=at,
        )
    base = f"/api/v1/organizations/{organization.id}/conversations"
    first = client.get(base, {"limit": 2})
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor and "{" not in cursor
    second = client.get(base, {"limit": 2, "cursor": cursor})
    assert second.status_code == 200
    invalid = client.get(base, {"cursor": "not-a-valid-cursor"})
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_cursor"
