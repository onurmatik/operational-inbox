from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    Domain,
    DomainDNSRecord,
    DomainTest,
    Notification,
    OutboundMessage,
    User,
)
from inbox.services.compose import send_composed_email
from inbox.services.domains import (
    apply_domain_readiness,
    create_domain,
    enable_receiving,
    provision_inbound,
    provision_outbound_identity,
)
from inbox.services.jobs import request_outbound_provisioning
from inbox.services.receipt_rules import receipt_allowlist
from tests.legacy_helpers import archived_record


@pytest.mark.django_db
def test_notification_compatibility_merges_live_and_archived_records(client, owner, domain):
    client.force_login(owner)
    at = timezone.now()
    historical = archived_record(
        domain,
        "Notification",
        created_at=at,
        id=uuid.UUID(int=10),
        kind="legacy",
        status="SENT",
        title="Historical",
        body="Old notification",
    )
    live = Notification.objects.create(
        domain=domain,
        id=uuid.UUID(int=20),
        channel="EMAIL",
        kind="delivery",
        dedupe_key="mixed-page",
        status="SENT",
        title="Delivered",
        body="Current notification",
    )
    Notification.objects.filter(pk=live.pk).update(created_at=at)
    url = f"/api/v1/domains/{domain.id}/notifications"
    first = client.get(url, {"limit": 1}).json()
    assert first["items"][0]["id"] == str(live.id) and first["next_cursor"]
    second = client.get(url, {"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["id"] == str(historical.object_id)
    assert second["next_cursor"] is None
    assert client.post(f"{url}/{live.id}/read").status_code == 200
    assert client.get(url).json()["items"][0]["status"] == "READ"
    live.refresh_from_db()
    assert live.status == "SENT"  # Read compatibility must not alter delivery state.
    foreign_owner = User.objects.create_user(
        email="notification-outsider@example.net", email_verified_at=timezone.now()
    )
    client.force_login(foreign_owner)
    assert client.get(url).status_code == 404
    assert client.post(f"{url}/{live.id}/read").status_code == 404


@pytest.mark.django_db
def test_web_send_only_setup_renders_and_receiving_can_be_added(client, owner, monkeypatch):
    inspect = Mock(side_effect=AssertionError("Send-only must not inspect MX"))
    monkeypatch.setattr("inbox.services.domains.inspect_mx", inspect)
    client.force_login(owner)
    response = client.post(
        reverse("domain_create"),
        {
            "hostname": "web-outreach.example",
            "goal": "send",
            "setup_mode": "DIRECT_MX",
        },
        follow=True,
    )
    assert response.status_code == 200
    assert (
        b"Enable receiving" in response.content
        and b"Preparing your DNS instructions" in response.content
    )
    domain = Domain.objects.get(hostname="web-outreach.example")
    assert not domain.receiving_enabled and domain.sending_requested
    inspect.assert_not_called()
    domain.outbound_ready = True
    domain.outbound_status = "READY"
    domain.save()
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname, **kwargs: [])
    enabled = client.post(
        reverse("domain_enable_receiving", args=[domain.id]),
        {
            "setup_mode": "PROVIDER_FORWARD",
        },
        follow=True,
    )
    assert enabled.status_code == 200
    domain.refresh_from_db()
    assert domain.receiving_enabled and domain.outbound_ready and domain.outbound_status == "READY"


@pytest.mark.django_db
def test_send_only_verifies_ownership_and_dkim_without_receiving(owner, monkeypatch):
    inspect = Mock(side_effect=AssertionError("Send-only must not inspect MX"))
    monkeypatch.setattr("inbox.services.domains.inspect_mx", inspect)
    domain = create_domain(owner=owner, hostname="outreach.example", goal="send")
    assert not domain.receiving_enabled and domain.sending_requested
    assert not domain.inbound_routes.exists()
    inbound_ses = Mock()
    domain = provision_inbound(domain, ses_client=inbound_ses)
    assert not inbound_ses.mock_calls
    assert not domain.dns_records.filter(purpose="MX").exists()
    proof = domain.dns_records.get(purpose="OWNERSHIP")
    proof.status = "VALID"
    proof.save()
    apply_domain_readiness(domain)
    domain.refresh_from_db()
    assert domain.ownership_verified and not domain.inbound_ready
    domain, job, started = request_outbound_provisioning(domain)
    assert started and job.kind == "provision_outbound"
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "verification-proof"}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["a", "b", "c"]}
    domain = provision_outbound_identity(domain, ses_client=ses)
    assert not domain.dns_records.filter(purpose="MX").exists()
    assert domain.dns_records.filter(purpose="DKIM").count() == 3
    domain.dns_records.filter(purpose="DKIM").update(status="VALID")
    apply_domain_readiness(
        domain, ses_verification_status="SUCCESS", dkim_verification_status="SUCCESS"
    )
    domain.refresh_from_db()
    assert domain.outbound_ready and not domain.inbound_ready and not domain.tests.exists()
    assert domain.hostname not in receipt_allowlist()
    inspect.assert_not_called()


@pytest.mark.django_db
def test_sending_requires_current_ownership_proof(domain):
    domain.outbound_status = "DISABLED"
    domain.save()
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose="OWNERSHIP",
        record_type="TXT",
        name="_operational-inbox.example.com",
        value="proof",
        status="MISSING",
    )
    with pytest.raises(ValidationError, match="ownership"):
        request_outbound_provisioning(domain)


@pytest.mark.django_db
def test_receiving_can_be_added_without_resetting_sending(domain, monkeypatch):
    domain.receiving_enabled = False
    domain.save()
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname, **kwargs: [])
    old_generation = domain.inbound_setup_generation
    enabled, started = enable_receiving(domain, setup_mode="PROVIDER_FORWARD")
    assert started and enabled.receiving_enabled
    assert enabled.outbound_ready and enabled.outbound_status == "READY"
    assert enabled.inbound_setup_generation == old_generation + 1
    assert enabled.inbound_routes.get().kind == "FORWARDING_ALIAS"
    repeated, started = enable_receiving(enabled, setup_mode="PROVIDER_FORWARD")
    assert not started and repeated.inbound_setup_generation == enabled.inbound_setup_generation
    assert enabled.jobs.filter(kind="provision_domain").count() == 1


@pytest.mark.django_db
def test_conversations_cross_domains_ignore_session_but_enforce_ownership(
    client, owner, domain, inbound_message
):
    other_domain = Domain.objects.create(
        owner=owner, hostname="second.example", claim_expires_at=domain.claim_expires_at
    )
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(other_domain.id)
    session.save()
    assert (
        client.get(reverse("inbox")).context["conversations"][0].id
        == inbound_message.conversation_id
    )
    detail = client.get(reverse("conversation_detail", args=[inbound_message.conversation_id]))
    assert detail.status_code == 200 and inbound_message.text_body.encode() in detail.content
    assert (
        client.get(
            reverse("conversation_detail", args=[inbound_message.conversation_id]),
            {"domain": str(other_domain.id)},
        ).status_code
        == 404
    )
    response = client.post(
        reverse("conversation_action", args=[inbound_message.conversation_id]),
        {"action": "archive"},
    )
    assert response.status_code == 302
    inbound_message.conversation.refresh_from_db()
    assert inbound_message.conversation.archived_at
    foreign = User.objects.create_user(
        email="foreign@example.net", email_verified_at=timezone.now(), is_active=True
    )
    client.force_login(foreign)
    assert (
        client.post(
            reverse("conversation_action", args=[inbound_message.conversation_id]),
            {"action": "trash"},
        ).status_code
        == 404
    )
    assert client.get(reverse("inbox"), {"domain": "bad-id"}).status_code == 404


@pytest.mark.django_db
def test_queued_sent_conversation_is_immediate_and_test_mail_stays_out_of_inbox(
    client, owner, domain, inbound_message
):
    sent = send_composed_email(
        domain=domain,
        owner=owner,
        from_address="hello@example.com",
        to_address="customer@example.net",
        subject="A customer follow-up",
        body_text="Hello from your project",
        idempotency_key=uuid.uuid4(),
    )
    assert not sent.conversation.messages.exists()
    DomainTest.objects.create(
        domain=domain,
        address="test-fixture@example.com",
        token_hash="fixture",
        expires_at=timezone.now() + timedelta(days=1),
        status="RECEIVED",
        received_message=inbound_message,
    )
    client.force_login(owner)
    assert not client.get(reverse("inbox")).context["conversations"]
    page = client.get(
        reverse("inbox"), {"folder": "sent", "conversation": str(sent.conversation_id)}
    )
    assert page.status_code == 200
    assert page.context["conversations"][0].id == sent.conversation_id
    assert b"Queued" in page.content and b"Hello from your project" in page.content
    sent.conversation.archived_at = timezone.now()
    sent.conversation.save()
    assert (
        client.get(reverse("inbox"), {"folder": "archive"}).context["conversations"][0].id
        == sent.conversation_id
    )


@pytest.mark.django_db
def test_full_from_address_form_preserves_exact_send_and_idempotency(client, owner, domain):
    client.force_login(owner)
    payload = {
        "from_address": "support@example.com",
        "to_address": "customer@example.net",
        "subject": "An exact email",
        "body_text": "First line\nSecond line",
        "idempotency_key": str(uuid.uuid4()),
    }
    first = client.post(reverse("compose"), payload)
    second = client.post(reverse("compose"), payload)
    assert first.status_code == second.status_code == 302
    assert OutboundMessage.objects.count() == 1
    outbound = OutboundMessage.objects.get()
    assert outbound.from_address == payload["from_address"]
    assert outbound.body_text == payload["body_text"]
    assert outbound.revision.approval.content_hash == outbound.content_hash
    conflict = client.post(reverse("compose"), {**payload, "body_text": "Changed"})
    assert conflict.status_code == 409
    invalid = client.post(
        reverse("compose"), {**payload, "from_address": "support@foreign.example"}
    )
    assert "from_address" in invalid.context["form"].errors


@pytest.mark.django_db(transaction=True)
def test_retirement_migration_archives_and_restores_nonempty_history():
    executor = MigrationExecutor(connection)
    old_target = [("inbox", "0014_compose_email")]
    target = [("inbox", "0015_simplify_agent_first")]
    executor.migrate(old_target)
    try:
        old = executor.loader.project_state(old_target).apps
        now = timezone.now()
        user = old.get_model("inbox", "User").objects.create(
            email="migration@example.net", is_active=True
        )
        domain = old.get_model("inbox", "Domain").objects.create(
            owner=user,
            hostname="migration.example",
            timezone="Europe/Istanbul",
            claim_expires_at=now + timedelta(days=1),
            outbound_status="READY",
        )
        conversation = old.get_model("inbox", "Conversation").objects.create(
            domain=domain, first_message_at=now, last_message_at=now, snoozed_until=now
        )
        message = old.get_model("inbox", "Message").objects.create(
            domain=domain,
            conversation=conversation,
            from_address="sender@example.net",
            provider_message_id="migration",
            received_at=now,
            direction="INBOUND",
        )
        classification = old.get_model("inbox", "Classification").objects.create(
            domain=domain,
            message=message,
            source="OWNER",
            category="ACTIONABLE",
            summary="Archived content",
        )
        run = old.get_model("inbox", "AgentRun").objects.create(
            domain=domain,
            kind="REPORT",
            model_name="legacy",
            reasoning_effort="low",
            prompt_version="1",
            schema_version="1",
        )
        schedule = old.get_model("inbox", "ReportSchedule").objects.create(domain=domain)
        report = old.get_model("inbox", "Report").objects.create(
            domain=domain,
            agent_run=run,
            kind="DAILY",
            schedule_key="once",
            period_start=now,
            period_end=now,
            title="Historical report",
            content="Historical body",
        )
        item = old.get_model("inbox", "ReportItem").objects.create(
            domain=domain,
            report=report,
            classification=classification,
            conversation=conversation,
            rank=1,
            summary="Item",
        )
        notification = old.get_model("inbox", "Notification").objects.create(
            domain=domain,
            channel="IN_APP",
            status="READ",
            read_at=now,
            kind="legacy",
            dedupe_key="legacy",
            title="Notification",
        )
        email = old.get_model("inbox", "Notification").objects.create(
            domain=domain,
            channel="EMAIL",
            kind="security",
            dedupe_key="email",
            title="Security",
            status="READ",
            read_at=now,
        )
        job = old.get_model("inbox", "DurableJob").objects.create(
            domain=domain,
            kind="generate_report",
            idempotency_key="retired",
            due_at=now,
            status="LEASED",
            leased_until=now,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        new = executor.loader.project_state(target).apps
        Audit = new.get_model("inbox", "AuditEvent")
        for model_name, row in (
            ("Classification", classification),
            ("AgentRun", run),
            ("ReportSchedule", schedule),
            ("Report", report),
            ("ReportItem", item),
            ("Notification", notification),
        ):
            archived = Audit.objects.get(object_type=model_name, object_id=row.id)
            assert archived.metadata["record"]["id"] == str(row.id)
            assert archived.created_at == row.created_at
        assert new.get_model("inbox", "Domain").objects.get(pk=domain.id).sending_requested
        assert new.get_model("inbox", "Notification").objects.get().id == email.id
        assert new.get_model("inbox", "Notification").objects.get().status == "SENT"
        assert Audit.objects.filter(
            object_type="Notification", object_id=email.id, event_type="notification.read"
        ).exists()
        retired = new.get_model("inbox", "DurableJob").objects.get(pk=job.id)
        assert retired.status == "COMPLETE" and retired.leased_until is None
        executor = MigrationExecutor(connection)
        executor.migrate(old_target)
        restored = executor.loader.project_state(old_target).apps
        assert (
            restored.get_model("inbox", "ReportItem").objects.get(pk=item.id).classification_id
            == classification.id
        )
        assert (
            restored.get_model("inbox", "Report").objects.get(pk=report.id).content
            == "Historical body"
        )
        assert (
            restored.get_model("inbox", "Notification").objects.get(pk=notification.id).read_at
            == now
        )
        assert restored.get_model("inbox", "Notification").objects.get(pk=email.id).status == "READ"
        assert restored.get_model("inbox", "Notification").objects.get(pk=email.id).read_at == now
        assert (
            restored.get_model("inbox", "Domain").objects.get(pk=domain.id).timezone
            == "Europe/Istanbul"
        )
        assert restored.get_model("inbox", "DurableJob").objects.get(pk=job.id).status == "LEASED"
    finally:
        MigrationExecutor(connection).migrate(target)
