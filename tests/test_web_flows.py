from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from inbox.forms import APITokenForm, DomainForm, ScheduleForm
from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Classification,
    Conversation,
    Domain,
    DomainDNSRecord,
    DomainTest,
    InboundRoute,
    Notification,
    Organization,
    Project,
    Report,
)
from inbox.services.domains import MXObservation, build_dns_instructions


def _setup_domain(
    organization,
    project,
    *,
    hostname: str,
    status: str,
    setup_mode: str = Domain.SetupMode.DIRECT_MX,
) -> Domain:
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname=hostname,
        setup_mode=setup_mode,
        status=status,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    local_part = f"route-{domain.id.hex[:12]}"
    InboundRoute.objects.create(
        organization=organization,
        domain=domain,
        kind=(
            InboundRoute.Kind.DIRECT_DOMAIN
            if setup_mode == Domain.SetupMode.DIRECT_MX
            else InboundRoute.Kind.FORWARDING_ALIAS
        ),
        local_part=local_part,
        address=f"{local_part}@inbound.example.net",
    )
    return domain


def test_choice_widgets_do_not_receive_text_input_styles():
    domain_form = DomainForm()
    assert domain_form.fields["hostname"].widget.attrs["class"] == "oi-input"
    assert "class" not in domain_form.fields["setup_mode"].widget.attrs
    assert 'type="radio"' in str(domain_form["setup_mode"])
    assert 'type="radio" name="setup_mode" value="DIRECT_MX" class="oi-input"' not in str(
        domain_form["setup_mode"]
    )

    assert "class" not in ScheduleForm().fields["is_enabled"].widget.attrs
    assert "class" not in APITokenForm().fields["scopes"].widget.attrs


@pytest.mark.django_db
def test_domain_create_explains_the_routing_tradeoff(client, owner, organization, project):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session["pending_domain"] = "portfolio.fit"
    session.save()

    response = client.get(reverse("domain_create"))

    assert response.status_code == 200
    assert b"Route this domain directly" in response.content
    assert b"Keep your current email provider" in response.content
    assert b"requests@portfolio.fit" in response.content
    assert b"Check domain and continue" in response.content
    assert b"data-mx-inspect-url" in response.content
    assert b"data-mx-check" in response.content
    assert b"Choose a different setup" in response.content
    assert b"We check public MX records first" in response.content
    assert response.content.count(b'class="oi-choice-card"') == 2

    invalid_response = client.post(reverse("domain_create"), {"hostname": "portfolio.fit"})
    assert invalid_response.status_code == 200
    assert b"This field is required." in invalid_response.content


@pytest.mark.django_db
def test_domain_mx_inspection_recommends_and_caches_public_dns_result(
    client, owner, monkeypatch
):
    client.force_login(owner)
    cache.clear()
    calls = []

    def no_existing_mx(hostname):
        calls.append(hostname)
        return []

    monkeypatch.setattr("inbox.views.inspect_mx", no_existing_mx)
    url = reverse("domain_mx_inspect")
    first = client.post(url, {"hostname": "Portfolio.Fit."})
    second = client.post(url, {"hostname": "portfolio.fit"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {
        "hostname": "portfolio.fit",
        "has_existing_mx": False,
        "recommended_setup_mode": Domain.SetupMode.DIRECT_MX,
        "mx_records": [],
    }
    assert calls == ["portfolio.fit"]
    assert "no-store" in first.headers["Cache-Control"]


@pytest.mark.django_db
def test_domain_mx_inspection_preserves_existing_mail_and_surfaces_dns_failures(
    client, owner, monkeypatch
):
    client.force_login(owner)
    cache.clear()
    monkeypatch.setattr(
        "inbox.views.inspect_mx",
        lambda hostname: [MXObservation(10, "mx1.example.net")],
    )
    url = reverse("domain_mx_inspect")
    existing = client.post(url, {"hostname": "mail.example.org"})

    assert existing.status_code == 200
    assert existing.json()["recommended_setup_mode"] == Domain.SetupMode.PROVIDER_FORWARD
    assert existing.json()["mx_records"] == [
        {"preference": 10, "exchange": "mx1.example.net"}
    ]
    assert client.get(url, {"hostname": "mail.example.org"}).status_code == 405

    def unavailable(hostname):
        raise ValidationError("The MX lookup timed out. Try again shortly.")

    monkeypatch.setattr("inbox.views.inspect_mx", unavailable)
    failed = client.post(url, {"hostname": "timeout.example.org"})
    invalid = client.post(url, {"hostname": "not-a-domain"})

    assert failed.status_code == 503
    assert failed.json()["code"] == "mx_lookup_failed"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_hostname"


@pytest.mark.django_db
def test_domain_detail_provisioning_waits_and_auto_polls_without_actions(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="preparing.example.org",
        status=Domain.Status.PROVISIONING,
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Preparing your DNS instructions" in response.content
    assert b"nothing for you to change yet" in response.content
    assert b'aria-live="polite"' in response.content
    assert b'aria-busy="true"' in response.content
    assert b"data-provisioning-poll" in response.content
    assert f"/domains/{domain.id}".encode() in response.content
    assert b"domain.error?.message" in response.content
    assert b"domain.error ||" not in response.content
    assert b'id="dns-check-button"' not in response.content
    assert b"Generate test address" not in response.content
    assert b"DNS records to add" not in response.content


@pytest.mark.django_db
def test_domain_detail_pending_dns_shows_exact_records_and_direct_mx_warning(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="dns.example.org",
        status=Domain.Status.PENDING_DNS,
    )
    build_dns_instructions(
        domain,
        verification_token="ownership-proof",
        dkim_tokens=["dkim-one"],
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Update DNS for dns.example.org" in response.content
    assert b"cannot change them for you" in response.content
    assert b"incoming email for every address" in response.content
    assert b"I've updated DNS" in response.content
    assert b"_amazonses.dns.example.org" in response.content
    assert b"ownership-proof" in response.content
    assert b"inbound-smtp.us-east-1.amazonaws.com" in response.content
    assert b"dkim-one._domainkey.dns.example.org" in response.content
    assert b"dkim-one.dkim.amazonses.com" in response.content
    assert b"Required for sending" in response.content
    assert b"Generate test address" not in response.content


@pytest.mark.django_db
def test_domain_detail_pending_test_enables_delivery_test(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="test-ready.example.org",
        status=Domain.Status.PENDING_TEST,
    )
    domain.ownership_verified = True
    domain.save(update_fields=("ownership_verified", "updated_at"))
    build_dns_instructions(
        domain,
        verification_token="ownership-proof",
        dkim_tokens=["dkim-one"],
    )
    domain.dns_records.filter(is_required=True).update(
        status=DomainDNSRecord.Status.VALID
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Confirm delivery with a real email" in response.content
    assert b"Generate test address" in response.content
    assert b"Check DNS again" in response.content

    session = client.session
    session[f"domain_test_address:{domain.id}"] = {
        "address": "test-private@test-ready.example.org",
        "expires_at": (timezone.now() + timedelta(hours=1)).timestamp(),
    }
    session.save()
    first_reveal = client.get(reverse("domain_detail", args=[domain.id]))
    second_reveal = client.get(reverse("domain_detail", args=[domain.id]))
    assert b"test-private@test-ready.example.org" in first_reveal.content
    assert b"test-private@test-ready.example.org" in second_reveal.content
    assert b"Generate test address" not in second_reveal.content


@pytest.mark.django_db
def test_web_rejects_premature_domain_test_creation(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="not-ready.example.org",
        status=Domain.Status.PENDING_DNS,
    )

    response = client.post(
        reverse("domain_create_test", args=[domain.id]),
        follow=True,
    )

    assert response.status_code == 200
    assert b"Verify the required DNS records" in response.content
    assert not DomainTest.objects.filter(domain=domain).exists()
    assert not AuditEvent.objects.filter(
        organization=organization,
        event_type="domain.test_created",
        object_id=domain.id,
    ).exists()


@pytest.mark.django_db
def test_domain_detail_error_hides_inactive_forwarding_and_dns_instructions(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["organization_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="failed-forward.example.org",
        status=Domain.Status.ERROR,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    build_dns_instructions(
        domain,
        verification_token="ownership-proof",
        dkim_tokens=["dkim-one"],
    )
    domain.error_code = "domain_provision_failed"
    domain.error_message = "Setup could not be completed."
    domain.save(update_fields=("error_code", "error_message", "updated_at"))
    route = domain.inbound_routes.get()

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Setup could not be completed." in response.content
    assert b"Provider catch-all forwarding" not in response.content
    assert route.address.encode() not in response.content
    assert b"DNS records to add" not in response.content
    assert b"ownership-proof" not in response.content


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
