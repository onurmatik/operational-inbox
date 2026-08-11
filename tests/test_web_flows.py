from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from inbox.forms import DomainForm
from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Conversation,
    Domain,
    DomainDNSRecord,
    DomainTest,
    DurableJob,
    InboundRoute,
    InboundRoutingTransition,
    Message,
    MessageRecipient,
    OutboundMessage,
    ReplyDraft,
)
from inbox.services.domains import (
    DomainClaimLookupError,
    MXObservation,
    build_dns_instructions,
    build_inbound_dns_instructions,
    classify_domain_routing,
    ensure_domain_test,
)
from inbox.services.drafts import revise_draft


def _setup_domain(
    organization,
    project,
    *,
    hostname: str,
    status: str,
    setup_mode: str = Domain.SetupMode.DIRECT_MX,
) -> Domain:
    domain = Domain.objects.create(
        owner=project.owner,
        hostname=hostname,
        setup_mode=setup_mode,
        status=status,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    local_part = f"route-{domain.id.hex[:12]}"
    InboundRoute.objects.create(
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


@pytest.mark.django_db
def test_domain_create_explains_the_routing_tradeoff(client, owner, organization, project):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
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
    assert b"Choose a different setup" not in response.content
    assert b"We check public MX records first" in response.content
    assert b"Direct routing to Operational Inbox" in response.content
    assert b"Route this domain's MX records directly to Operational Inbox" in response.content
    assert b"Amazon SES" not in response.content
    assert b"Direct SES MX" not in response.content
    assert response.content.count(b'class="oi-choice-card"') == 2

    invalid_response = client.post(reverse("domain_create"), {"hostname": "portfolio.fit"})
    assert invalid_response.status_code == 200
    assert b"This field is required." in invalid_response.content


@pytest.mark.django_db
def test_domain_mx_inspection_recommends_and_caches_public_dns_result(client, owner, monkeypatch):
    client.force_login(owner)
    cache.clear()
    calls = []

    def no_existing_mx(hostname):
        calls.append(hostname)
        return classify_domain_routing([], has_operational_inbox_claim=None)

    monkeypatch.setattr("inbox.views.inspect_domain_routing", no_existing_mx)
    url = reverse("domain_mx_inspect")
    first = client.post(url, {"hostname": "Portfolio.Fit."})
    second = client.post(url, {"hostname": "portfolio.fit"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {
        "hostname": "portfolio.fit",
        "has_existing_mx": False,
        "mx_classification": "NO_MX",
        "has_operational_inbox_claim": None,
        "recommended_setup_mode": Domain.SetupMode.DIRECT_MX,
        "requires_explicit_choice": False,
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
        "inbox.views.inspect_domain_routing",
        lambda hostname: classify_domain_routing(
            [MXObservation(10, "mx1.example.net")],
            has_operational_inbox_claim=False,
        ),
    )
    url = reverse("domain_mx_inspect")
    existing = client.post(url, {"hostname": "mail.example.org"})

    assert existing.status_code == 200
    assert existing.json()["recommended_setup_mode"] == Domain.SetupMode.PROVIDER_FORWARD
    assert existing.json()["mx_classification"] == "EXTERNAL_MX"
    assert existing.json()["mx_records"] == [{"preference": 10, "exchange": "mx1.example.net"}]
    assert client.get(url, {"hostname": "mail.example.org"}).status_code == 405

    def unavailable(hostname):
        raise ValidationError("The MX lookup timed out. Try again shortly.")

    monkeypatch.setattr("inbox.views.inspect_domain_routing", unavailable)
    failed = client.post(url, {"hostname": "timeout.example.org"})
    invalid = client.post(url, {"hostname": "not-a-domain"})

    assert failed.status_code == 503
    assert failed.json()["code"] == "mx_lookup_failed"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_hostname"

    def claim_unavailable(hostname):
        raise DomainClaimLookupError("The ownership-record lookup timed out.")

    monkeypatch.setattr("inbox.views.inspect_domain_routing", claim_unavailable)
    claim_failed = client.post(url, {"hostname": "claim-timeout.example.org"})
    assert claim_failed.status_code == 503
    assert claim_failed.json()["code"] == "claim_lookup_failed"


@pytest.mark.django_db
def test_domain_mx_inspection_recognizes_reconnect_and_requires_ambiguous_choices(
    client, owner, monkeypatch
):
    client.force_login(owner)
    cache.clear()

    def inspect(hostname):
        if hostname == "reconnect.example.org":
            return classify_domain_routing(
                [MXObservation(10, "inbound-smtp.us-east-1.amazonaws.com")],
                has_operational_inbox_claim=True,
            )
        if hostname == "shared-ses.example.org":
            return classify_domain_routing(
                [MXObservation(5, "INBOUND-SMTP.US-EAST-1.AMAZONAWS.COM.")],
                has_operational_inbox_claim=False,
            )
        return classify_domain_routing(
            [
                MXObservation(10, "inbound-smtp.us-east-1.amazonaws.com"),
                MXObservation(20, "mx.provider.example"),
            ],
            has_operational_inbox_claim=True,
        )

    monkeypatch.setattr("inbox.views.inspect_domain_routing", inspect)
    url = reverse("domain_mx_inspect")

    reconnect = client.post(url, {"hostname": "reconnect.example.org"}).json()
    shared_ses = client.post(url, {"hostname": "shared-ses.example.org"}).json()
    mixed = client.post(url, {"hostname": "mixed.example.org"}).json()

    assert reconnect["mx_classification"] == "OPERATIONAL_INBOX_RECONNECT"
    assert reconnect["recommended_setup_mode"] == Domain.SetupMode.DIRECT_MX
    assert reconnect["has_operational_inbox_claim"] is True
    assert reconnect["requires_explicit_choice"] is False
    assert shared_ses["mx_classification"] == "SES_MX_UNCLAIMED"
    assert shared_ses["recommended_setup_mode"] is None
    assert shared_ses["requires_explicit_choice"] is True
    assert mixed["mx_classification"] == "MIXED_MX"
    assert mixed["recommended_setup_mode"] is None
    assert mixed["requires_explicit_choice"] is True


@pytest.mark.django_db
def test_domain_detail_provisioning_waits_and_auto_polls_without_actions(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
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
    session["domain_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="dns.example.org",
        status=Domain.Status.PENDING_DNS,
    )
    build_inbound_dns_instructions(
        domain,
        ownership_token="ownership-proof",
        verification_token="ses-proof",
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Update DNS for dns.example.org" in response.content
    assert b"cannot change them for you" in response.content
    assert b"incoming email for every address" in response.content
    assert b"I've updated DNS" in response.content
    assert b"_operational-inbox-claim.dns.example.org" in response.content
    assert b"ownership-proof" in response.content
    assert b"_amazonses.dns.example.org" in response.content
    assert b"ses-proof" in response.content
    assert b"inbound-smtp.us-east-1.amazonaws.com" in response.content
    assert b"._domainkey.dns.example.org" not in response.content
    assert b"dkim.amazonses.com" not in response.content
    assert b"Operational Inbox verification" in response.content
    assert b"SES verification" not in response.content
    assert b"Amazon SES" not in response.content
    assert b"Required for direct receiving" in response.content
    assert b"Not enabled" in response.content
    assert b"Generate test address" not in response.content


@pytest.mark.django_db
def test_provider_forward_inbound_detail_shows_only_claim_and_private_route(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="forward-only.example.org",
        status=Domain.Status.PENDING_DNS,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    build_inbound_dns_instructions(domain, ownership_token="fresh-claim")
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.MX,
        record_type="MX",
        name=domain.hostname,
        value="inbound-smtp.us-east-1.amazonaws.com",
        priority=10,
        is_required=False,
    )
    route = domain.inbound_routes.get()

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert route.address.encode() in response.content
    assert b"_operational-inbox-claim.forward-only.example.org" in response.content
    assert b"_amazonses.forward-only.example.org" not in response.content
    assert b"inbound-smtp.us-east-1.amazonaws.com" not in response.content
    assert b"._domainkey.forward-only.example.org" not in response.content
    assert b"Amazon SES" not in response.content


@pytest.mark.django_db
def test_receiving_route_panel_starts_staged_change_without_cutting_over(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="reconnect.example.org",
        status=Domain.Status.READY,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    domain.ownership_verified = True
    domain.inbound_ready = True
    domain.save(update_fields=("ownership_verified", "inbound_ready", "updated_at"))
    previous_route = domain.inbound_routes.get()

    detail = client.get(reverse("domain_detail", args=[domain.id]))
    switch_url = reverse("domain_routing_transition_start", args=[domain.id])
    get_switch = client.get(switch_url)
    switched = client.post(
        switch_url,
        {"target_mode": Domain.SetupMode.DIRECT_MX},
        follow=True,
    )
    duplicate = client.post(
        switch_url,
        {"target_mode": Domain.SetupMode.DIRECT_MX},
        follow=True,
    )

    domain.refresh_from_db()
    previous_route.refresh_from_db()
    transition = domain.routing_transitions.get()
    target_route = transition.routes.get()
    assert detail.status_code == 200
    assert b"Receiving route" in detail.content
    assert b"Active" in detail.content
    assert b"Provider catch-all forwarding" in detail.content
    assert b'id="forwarding-route"' in detail.content
    assert b"Change to direct MX routing" in detail.content
    assert b"current route stays active throughout preparation" in detail.content
    assert b"fresh real email through the target path" in detail.content
    assert b"24-hour grace period" in detail.content
    assert b"Outbound sending is unaffected" in detail.content
    assert get_switch.status_code == 405
    assert switched.status_code == 200
    assert b"Receiving route change started" in switched.content
    assert b"Route change in progress" in switched.content
    assert b"Target: Direct MX routing" in switched.content
    assert "1 · Prepare".encode() in switched.content
    assert "2 · Verify DNS".encode() in switched.content
    assert "3 · Test target".encode() in switched.content
    assert "4 · Cut over".encode() in switched.content
    assert b"Cancel route change" in switched.content
    assert b"Current fallback alias" in switched.content
    assert b'id="current-fallback-alias"' in switched.content
    assert b'data-copy="#current-fallback-alias"' in switched.content
    assert previous_route.address.encode() in switched.content
    assert b'id="forwarding-route"' not in switched.content
    assert b"data-routing-transition-poll" in switched.content
    assert b'data-transition-status="PREPARING"' in switched.content
    assert b"already in progress" in duplicate.content
    assert domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert domain.inbound_setup_generation == 1
    assert transition.generation == 2
    assert domain.status == Domain.Status.READY
    assert previous_route.is_active
    assert transition.status == InboundRoutingTransition.Status.PREPARING
    assert transition.from_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert transition.to_mode == Domain.SetupMode.DIRECT_MX
    assert target_route.kind == InboundRoute.Kind.DIRECT_DOMAIN
    assert target_route.is_active
    assert Domain.objects.get(id=domain.id).outbound_status == Domain.OutboundStatus.DISABLED
    job = DurableJob.objects.get(kind="provision_routing_transition")
    assert job.payload == {
        "transition_id": str(transition.id),
        "generation": transition.generation,
    }
    assert AuditEvent.objects.filter(
        domain=domain,
        event_type="domain.routing_transition_started",
        object_id=transition.id,
    ).exists()


@pytest.mark.django_db
def test_provider_target_alias_is_visible_and_pre_cutover_change_can_be_cancelled(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="route-to-provider.example.org",
        status=Domain.Status.READY,
    )
    current_route = domain.inbound_routes.get()
    start_url = reverse("domain_routing_transition_start", args=[domain.id])
    client.post(start_url, {"target_mode": Domain.SetupMode.PROVIDER_FORWARD})
    transition = domain.routing_transitions.get()
    target_route = transition.routes.get()
    transition.status = InboundRoutingTransition.Status.WAITING_DNS
    transition.save(update_fields=("status", "updated_at"))

    detail = client.get(reverse("domain_detail", args=[domain.id]))
    cancel_url = reverse("domain_routing_transition_cancel", args=[domain.id])
    get_cancel = client.get(cancel_url)
    cancelled = client.post(cancel_url, follow=True)

    transition.refresh_from_db()
    target_route.refresh_from_db()
    current_route.refresh_from_db()
    assert detail.status_code == 200
    assert b"Target: Provider catch-all forwarding" in detail.content
    assert b"Restore or publish that provider's MX records" in detail.content
    assert b"complete both steps before testing" in detail.content
    assert b"Target provider forwarding alias" in detail.content
    assert target_route.address.encode() in detail.content
    assert b'data-copy="#transition-forwarding-route"' in detail.content
    assert b"does not update external DNS" in detail.content
    assert b"restore the single Operational Inbox MX record" in detail.content
    assert b"Cancel route change" in detail.content
    assert get_cancel.status_code == 405
    assert cancelled.status_code == 200
    assert b"Receiving route change cancelled" in cancelled.content
    assert b"restore the single Operational Inbox MX record in DNS" in cancelled.content
    assert b"Change to provider forwarding" in cancelled.content
    assert transition.status == InboundRoutingTransition.Status.CANCELLED
    assert not target_route.is_active
    assert current_route.is_active
    assert domain.setup_mode == Domain.SetupMode.DIRECT_MX
    assert AuditEvent.objects.filter(
        domain=domain,
        event_type="domain.routing_transition_cancelled",
        object_id=transition.id,
    ).exists()


@pytest.mark.django_db
def test_grace_progress_shows_cutover_and_no_longer_offers_cancel(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="route-grace.example.org",
        status=Domain.Status.READY,
    )
    client.post(
        reverse("domain_routing_transition_start", args=[domain.id]),
        {"target_mode": Domain.SetupMode.PROVIDER_FORWARD},
    )
    transition = domain.routing_transitions.get()
    grace_until = timezone.now() + timedelta(hours=24)
    transition.status = InboundRoutingTransition.Status.GRACE
    transition.cutover_at = timezone.now()
    transition.grace_until = grace_until
    transition.save(update_fields=("status", "cutover_at", "grace_until", "updated_at"))
    Domain.objects.filter(id=domain.id).update(
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        inbound_setup_generation=transition.generation,
    )

    detail = client.get(reverse("domain_detail", args=[domain.id]))

    assert detail.status_code == 200
    assert "Complete · grace active".encode() in detail.content
    assert b"Cutover is complete" in detail.content
    assert b"Cancel route change" not in detail.content
    assert b"Change to direct MX routing" not in detail.content
    assert b"Prepare change back to direct MX routing" in detail.content
    assert b'id="transition-forwarding-route"' in detail.content
    assert b'id="forwarding-route"' not in detail.content


@pytest.mark.django_db
def test_transition_dns_and_target_test_actions_remain_visible_for_ready_domain(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="ready-route-change.example.org",
        status=Domain.Status.READY,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    client.post(
        reverse("domain_routing_transition_start", args=[domain.id]),
        {"target_mode": Domain.SetupMode.DIRECT_MX},
    )
    transition = domain.routing_transitions.get()
    transition.status = InboundRoutingTransition.Status.WAITING_DNS
    transition.save(update_fields=("status", "updated_at"))

    waiting_dns = client.get(reverse("domain_detail", args=[domain.id]))
    assert "I've updated target DNS — Check records".encode() in waiting_dns.content
    assert b"Current fallback alias" in waiting_dns.content
    assert b"does not update external DNS" in waiting_dns.content
    assert b"restore your mail provider's MX records" in waiting_dns.content
    assert b'id="forwarding-route"' not in waiting_dns.content

    transition.status = InboundRoutingTransition.Status.WAITING_TEST
    transition.save(update_fields=("status", "updated_at"))
    target_test = DomainTest.objects.create(
        domain=domain,
        routing_transition=transition,
        setup_generation=transition.generation,
        expected_setup_mode=transition.to_mode,
        expected_route_kind=InboundRoute.Kind.DIRECT_DOMAIN,
        address=f"test-target@{domain.hostname}",
        token_hash="e" * 64,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    address = str(target_test.address)
    revealed_again = client.get(reverse("domain_detail", args=[domain.id]))
    second_client = Client()
    second_client.force_login(owner)
    revealed_in_another_session = second_client.get(reverse("domain_detail", args=[domain.id]))

    assert revealed_again.status_code == 200
    assert b"Check target DNS again" in revealed_again.content
    assert b"Generate target-path test" not in revealed_again.content
    assert address.encode() in revealed_again.content
    assert b"fresh message through the target route" in revealed_again.content
    assert revealed_again.content.count(b"Confirm delivery with a real email") == 1
    assert b"data-target-route-test-action" in revealed_again.content
    assert revealed_again.content.index(
        b"data-target-route-test-action"
    ) < revealed_again.content.index(b'id="receiving-route-title"')
    assert revealed_again.content.count(b"Open email app") == 1
    assert address.encode() in revealed_in_another_session.content
    assert b"data-routing-transition-poll" in revealed_again.content

    transition.status = InboundRoutingTransition.Status.WAITING_DNS
    transition.save(update_fields=("status", "updated_at"))
    regressed = client.get(reverse("domain_detail", args=[domain.id]))
    transition.status = InboundRoutingTransition.Status.WAITING_TEST
    transition.save(update_fields=("status", "updated_at"))
    ready_again = client.get(reverse("domain_detail", args=[domain.id]))

    assert address.encode() not in regressed.content
    assert address.encode() in ready_again.content
    assert b"Generate target-path test" not in ready_again.content

    cancelled = client.post(
        reverse("domain_routing_transition_cancel", args=[domain.id]),
        follow=True,
    )
    assert b"External DNS was not changed" in cancelled.content
    assert b"restore your mail provider" in cancelled.content
    assert b'id="forwarding-route"' in cancelled.content
    assert b'id="current-fallback-alias"' not in cancelled.content


@pytest.mark.django_db
def test_transition_waiting_test_replaces_pending_domain_cta_with_one_top_action(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="pending-test-route-change.example.org",
        status=Domain.Status.PENDING_TEST,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    client.post(
        reverse("domain_routing_transition_start", args=[domain.id]),
        {"target_mode": Domain.SetupMode.DIRECT_MX},
    )
    transition = domain.routing_transitions.get()
    transition.status = InboundRoutingTransition.Status.WAITING_TEST
    transition.save(update_fields=("status", "updated_at"))
    target_test = DomainTest.objects.create(
        domain=domain,
        routing_transition=transition,
        setup_generation=transition.generation,
        expected_setup_mode=transition.to_mode,
        expected_route_kind=InboundRoute.Kind.DIRECT_DOMAIN,
        address=f"test-target@{domain.hostname}",
        token_hash="f" * 64,
        expires_at=timezone.now() + timedelta(hours=24),
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert response.content.count(b"Confirm delivery with a real email") == 1
    assert response.content.count(b"Open email app") == 1
    assert response.content.count(b'id="transition-test-address"') == 1
    assert target_test.address.encode() in response.content
    assert b"data-target-route-test-action" in response.content
    assert response.content.count(b"data-inbound-test-poll") == 1


@pytest.mark.django_db
def test_provider_fallback_alias_remains_visible_during_direct_mx_grace(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="provider-fallback-grace.example.org",
        status=Domain.Status.READY,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    source_route = domain.inbound_routes.get()
    client.post(
        reverse("domain_routing_transition_start", args=[domain.id]),
        {"target_mode": Domain.SetupMode.DIRECT_MX},
    )
    transition = domain.routing_transitions.get()
    grace_until = timezone.now() + timedelta(hours=24)
    transition.status = InboundRoutingTransition.Status.GRACE
    transition.cutover_at = timezone.now()
    transition.grace_until = grace_until
    transition.save(update_fields=("status", "cutover_at", "grace_until", "updated_at"))
    source_route.grace_until = grace_until
    source_route.save(update_fields=("grace_until", "updated_at"))
    Domain.objects.filter(id=domain.id).update(
        setup_mode=Domain.SetupMode.DIRECT_MX,
        inbound_setup_generation=transition.generation,
    )

    detail = client.get(reverse("domain_detail", args=[domain.id]))

    assert detail.status_code == 200
    assert b"Current fallback alias" in detail.content
    assert b"This previous provider-forwarding route remains available until" in detail.content
    assert source_route.address.encode() in detail.content
    assert b'data-copy="#current-fallback-alias"' in detail.content
    assert b'id="forwarding-route"' not in detail.content


@pytest.mark.django_db
def test_legacy_direct_switch_url_starts_the_generic_transition(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="legacy-direct-change.example.org",
        status=Domain.Status.READY,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )

    response = client.post(reverse("domain_switch_to_direct", args=[domain.id]))

    assert response.status_code == 302
    transition = domain.routing_transitions.get()
    assert transition.to_mode == Domain.SetupMode.DIRECT_MX
    assert DurableJob.objects.filter(
        kind="provision_routing_transition",
        payload__transition_id=str(transition.id),
    ).exists()


@pytest.mark.django_db
def test_verified_direct_detail_does_not_ask_for_another_ownership_claim(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="verified-reconnect.example.org",
        status=Domain.Status.READY,
    )
    domain.ownership_verified = True
    domain.inbound_ready = True
    domain.existing_mx = [
        {
            "preference": 10,
            "exchange": "inbound-smtp.us-east-1.amazonaws.com",
        }
    ]
    domain.save(
        update_fields=(
            "ownership_verified",
            "inbound_ready",
            "existing_mx",
            "updated_at",
        )
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"this domain's ownership claim is verified" in response.content
    assert b"require a fresh ownership claim before activation" not in response.content


@pytest.mark.django_db
def test_ready_inbound_can_enable_sending_without_changing_receiving(
    client, owner, organization, project
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="enable-sending.example.org",
        status=Domain.Status.READY,
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    domain.inbound_ready = True
    domain.ownership_verified = True
    domain.save(update_fields=("inbound_ready", "ownership_verified", "updated_at"))
    build_inbound_dns_instructions(domain, ownership_token="fresh-claim")
    domain.dns_records.update(status=DomainDNSRecord.Status.VALID)
    url = reverse("domain_enable_outbound", args=[domain.id])

    detail = client.get(reverse("domain_detail", args=[domain.id]))
    get_response = client.get(url)
    enabled = client.post(url, follow=True)

    domain.refresh_from_db()
    assert b"Not enabled" in detail.content
    assert b"Enable sending" in detail.content
    assert get_response.status_code == 405
    assert enabled.status_code == 200
    assert b"Preparing sending DNS records" in enabled.content
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert domain.outbound_status == Domain.OutboundStatus.PROVISIONING
    assert (
        DurableJob.objects.filter(
            kind="provision_outbound", payload__domain_id=str(domain.id)
        ).count()
        == 1
    )
    assert AuditEvent.objects.filter(
        event_type="domain.outbound_provision_requested", object_id=domain.id
    ).exists()


@pytest.mark.django_db
def test_domain_detail_pending_test_enables_delivery_test(client, owner, organization, project):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="test-ready.example.org",
        status=Domain.Status.PENDING_TEST,
    )
    domain.ownership_verified = True
    domain.ses_identity_status = "SUCCESS"
    domain.save(update_fields=("ownership_verified", "ses_identity_status", "updated_at"))
    build_dns_instructions(
        domain,
        ownership_token="ownership-proof",
        verification_token="ownership-proof",
        dkim_tokens=["dkim-one"],
    )
    domain.dns_records.filter(is_required=True).update(status=DomainDNSRecord.Status.VALID)
    pending_test, address, created = ensure_domain_test(
        domain,
        receipt_rule_reconciler=lambda: None,
    )

    response = client.get(reverse("domain_detail", args=[domain.id]))
    second_client = Client()
    second_client.force_login(owner)
    second_response = second_client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert created is True
    assert pending_test.address == address
    assert b"Confirm delivery with a real email" in response.content
    assert b"Generate test address" not in response.content
    assert b"data-domain-test-form" not in response.content
    assert b"Check DNS again" in response.content
    assert b"data-inbound-test-poll" in response.content
    assert b"data-inbound-test-status" in response.content
    assert b'domain.inbound_ready || domain.status !== "PENDING_TEST"' in response.content
    assert b"From any external email account" in response.content
    assert b"You do not need to create a new mailbox" in response.content
    assert b"This page updates automatically" in response.content
    assert b"Open email app" in response.content
    assert b"Copy address" in response.content
    assert address.encode() in response.content
    assert address.encode() in second_response.content
    assert DomainTest.objects.filter(domain=domain, status=DomainTest.Status.PENDING).count() == 1


@pytest.mark.django_db
def test_web_rejects_premature_domain_test_creation(client, owner, organization, project):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
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
        domain=organization,
        event_type="domain.test_created",
        object_id=domain.id,
    ).exists()


@pytest.mark.django_db
def test_domain_disable_web_expires_every_pending_delivery_test(
    client,
    owner,
    organization,
    project,
):
    client.force_login(owner)
    domain = _setup_domain(
        organization,
        project,
        hostname="disable-tests-web.example.org",
        status=Domain.Status.PENDING_TEST,
    )
    transition = InboundRoutingTransition.objects.create(
        domain=domain,
        generation=2,
        from_mode=Domain.SetupMode.DIRECT_MX,
        to_mode=Domain.SetupMode.PROVIDER_FORWARD,
        from_domain_status=Domain.Status.PENDING_TEST,
        status=InboundRoutingTransition.Status.WAITING_TEST,
    )
    initial_test = DomainTest.objects.create(
        domain=domain,
        setup_generation=1,
        expected_setup_mode=Domain.SetupMode.DIRECT_MX,
        expected_route_kind=InboundRoute.Kind.DIRECT_DOMAIN,
        address="test-initial-disable-web@disable-tests-web.example.org",
        token_hash="3" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    transition_test = DomainTest.objects.create(
        domain=domain,
        routing_transition=transition,
        setup_generation=2,
        expected_setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        expected_route_kind=InboundRoute.Kind.FORWARDING_ALIAS,
        address="test-transition-disable-web@disable-tests-web.example.org",
        token_hash="4" * 64,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    response = client.post(reverse("domain_disable", args=[domain.id]), follow=True)

    assert response.status_code == 200
    initial_test.refresh_from_db()
    transition_test.refresh_from_db()
    transition.refresh_from_db()
    assert initial_test.status == DomainTest.Status.EXPIRED
    assert transition_test.status == DomainTest.Status.EXPIRED
    assert transition.status == InboundRoutingTransition.Status.CANCELLED
    assert not domain.inbound_routes.filter(is_active=True).exists()


@pytest.mark.django_db
def test_domain_detail_error_hides_inactive_forwarding_and_dns_instructions(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
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
        ownership_token="ownership-proof",
        verification_token="ownership-proof",
        dkim_tokens=["dkim-one"],
    )
    domain.error_code = "domain_provision_failed"
    domain.error_message = "Setup could not be completed."
    domain.save(update_fields=("error_code", "error_message", "updated_at"))
    route = domain.inbound_routes.get()

    response = client.get(reverse("domain_detail", args=[domain.id]))

    assert response.status_code == 200
    assert b"Setup could not be completed." not in response.content
    assert b"Operational Inbox could not finish preparing this domain" in response.content
    assert b"existing SES identity" not in response.content
    assert b"Receiving route" in response.content
    assert b"Provider catch-all forwarding" in response.content
    assert route.address.encode() not in response.content
    assert b"DNS records to add" not in response.content
    assert b"ownership-proof" not in response.content


@pytest.mark.django_db
def test_domain_collision_explains_safe_recovery_and_retry_is_idempotent(
    client, owner, organization, project
):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    domain = _setup_domain(
        organization,
        project,
        hostname="existing-identity.example.org",
        status=Domain.Status.ERROR,
    )
    domain.error_code = "ses_identity_collision"
    domain.error_message = "This SES identity already exists."
    domain.save(update_fields=("error_code", "error_message", "updated_at"))

    detail = client.get(reverse("domain_detail", args=[domain.id]))

    assert detail.status_code == 200
    assert b"Your DNS and mail routing were not changed" in detail.content
    assert b"new, unique DNS ownership record" in detail.content
    assert b"Retry setup safely" in detail.content
    assert b"Support reference" in detail.content
    assert b"SES identity" not in detail.content
    assert b"ses_identity_collision" not in detail.content

    retry_url = reverse("domain_retry_provisioning", args=[domain.id])
    first = client.post(retry_url, follow=True)
    second = client.post(retry_url, follow=True)
    domain.refresh_from_db()

    assert first.status_code == 200
    assert b"Preparing your DNS instructions" in first.content
    assert b"Existing email settings will be inspected" in first.content
    assert b"Existing SES settings" not in first.content
    assert b"A setup retry is already in progress." in second.content
    assert domain.status == Domain.Status.PROVISIONING
    assert (
        DurableJob.objects.filter(
            kind="provision_domain",
            payload__domain_id=str(domain.id),
            status=DurableJob.Status.PENDING,
        ).count()
        == 1
    )
    assert (
        AuditEvent.objects.filter(
            domain=domain,
            event_type="domain.provision_retry_requested",
            object_id=domain.id,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_authenticated_application_pages_render(
    client, owner, organization, project, conversation, inbound_message
):
    client.force_login(owner)
    client.session["domain_id"] = str(organization.id)
    client.session.save()
    Domain.objects.create(
        owner=project.owner,
        hostname="example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        inbound_ready=True,
        outbound_ready=True,
        outbound_status=Domain.OutboundStatus.READY,
        ownership_verified=True,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    AuditEvent.objects.create(
        domain=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type="test.rendered",
        object_type="Conversation",
        object_id=conversation.id,
        request_id="web-test",
    )
    draft = ReplyDraft.objects.create(
        domain=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = revise_draft(
        draft=draft,
        owner=owner,
        subject="Re: Privacy request",
        body_text="Reviewable response.",
    )
    OutboundMessage.objects.create(
        domain=project,
        conversation=conversation,
        revision=revision,
        status=OutboundMessage.Status.UNKNOWN,
        from_address="requests@example.org",
        to_address="sender@example.net",
        subject=revision.subject,
        body_text=revision.body_text,
        content_hash=revision.content_hash,
        rfc_message_id="<web-provider-neutral@example.org>",
        error_code="ses_acceptance_unknown",
        error_message="SES acceptance could not be determined.",
    )
    routes = [
        reverse("dashboard"),
        reverse("agents"),
        reverse("inbox"),
        reverse("conversation_detail", args=[conversation.id]),
        reverse("domains"),
        reverse("domain_create"),
        reverse("retention_settings"),
        reverse("api_tokens"),
        reverse("audit"),
    ]
    for url in routes:
        response = client.get(url)
        assert response.status_code == 200, url
        assert b"Operational Inbox" in response.content

    dashboard = client.get(reverse("dashboard"))
    assert b"New messages" in dashboard.content
    assert b"Mailboxes" in dashboard.content
    assert b"Needs attention" not in dashboard.content
    assert b"Notifications" not in dashboard.content
    assert b"Domain readiness" not in dashboard.content
    assert b"Recent audited activity" not in dashboard.content
    assert b"Audit activity needs attention" not in dashboard.content

    domains = client.get(reverse("domains"))
    assert b"Direct routing to Operational Inbox" in domains.content
    assert b"Direct SES MX" not in domains.content

    detail = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert b"Operational Inbox could not confirm" in detail.content
    assert b"previous delivery outcome may be unknown" in detail.content
    assert b"SES acceptance" not in detail.content
    assert b"previous SES outcome" not in detail.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "route_name",
    [
        "retention_settings",
        "audit",
    ],
)
def test_domain_scoped_settings_redirect_when_an_active_domain_is_required(
    client, owner, route_name
):
    client.force_login(owner)

    response = client.get(reverse(route_name))

    assert response.status_code == 302
    assert response.url == reverse("domains")

    followed = client.get(reverse(route_name), follow=True)
    assert followed.status_code == 200
    assert followed.redirect_chain == [(reverse("domains"), 302)]
    assert b"Connect a domain to use" in followed.content
    assert b"Domain required" not in followed.content


@pytest.mark.django_db
def test_focused_rail_disables_domain_dependent_navigation_without_a_domain(client, owner):
    client.force_login(owner)

    response = client.get(reverse("domain_create"))

    assert response.status_code == 200
    assert b'id="settings-menu-button"' in response.content
    assert b'id="profile-menu-button"' in response.content
    assert b"No domain connected" in response.content
    assert response.content.count(b'aria-disabled="true"') == 4
    assert b"Requires a domain" in response.content
    assert reverse("domain_create").encode() in response.content
    assert b"Connect domain" in response.content
    assert reverse("retention_settings").encode() not in response.content
    assert reverse("api_tokens").encode() in response.content
    assert reverse("audit").encode() not in response.content
    assert reverse("agents").encode() in response.content

    agents = client.get(reverse("agents"))
    assert agents.status_code == 200
    assert b'aria-current="page"' in agents.content
    assert b"Use Operational Inbox with your agent" in agents.content
    assert b"Connect in two steps" in agents.content
    assert b"Copy agent prompt" in agents.content
    assert b"https://operationalinbox.com/INSTALL.md" in agents.content
    assert (
        b"onurmatik/operational-inbox/tree/main/.agents/skills/operational-inbox" in agents.content
    )
    assert b"Help me connect this agent to Operational Inbox" in agents.content
    assert b"$operational-inbox" in agents.content
    assert b"Native MCP" in agents.content
    assert reverse("install_instructions").encode() in agents.content
    assert reverse("mcp_docs").encode() in agents.content
    assert b"Standalone skill" in agents.content
    assert b"Prompt" in agents.content
    assert b"triage-inboxes" in agents.content
    assert b"reply-to-conversations" in agents.content
    assert b"setup-domain" in agents.content
    assert b"monitor-outbound-delivery" in agents.content
    assert agents.content.count(b"data-copy-target=") == 6
    assert reverse("api_tokens").encode() in agents.content


@pytest.mark.django_db
def test_dashboard_only_surfaces_readiness_and_audit_when_they_need_attention(
    client, owner, domain
):
    client.force_login(owner)
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=owner.id,
        event_type="conversation.viewed",
        object_type="Domain",
        object_id=domain.id,
        request_id="dashboard-normal-audit",
    )

    healthy = client.get(reverse("dashboard"))

    assert healthy.status_code == 200
    assert b"Domain readiness" not in healthy.content
    assert b"Recent audited activity" not in healthy.content
    assert b"Audit activity needs attention" not in healthy.content

    domain.status = Domain.Status.PENDING_DNS
    domain.inbound_ready = False
    domain.save(update_fields=("status", "inbound_ready", "updated_at"))

    pending = client.get(reverse("dashboard"))

    assert b"Domain readiness needs attention" in pending.content
    assert b"Current domain status: Pending DNS." in pending.content
    assert reverse("domain_detail", args=[domain.id]).encode() in pending.content
    assert b"Audit activity needs attention" not in pending.content

    domain.status = Domain.Status.READY
    domain.inbound_ready = True
    domain.save(update_fields=("status", "inbound_ready", "updated_at"))
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.AGENT,
        actor_id=owner.id,
        event_type="agent.draft_failed",
        object_type="AgentRun",
        request_id="dashboard-failed-audit",
        metadata={"error_code": "openai_error"},
    )

    failed_audit = client.get(reverse("dashboard"))

    assert b"Domain readiness needs attention" not in failed_audit.content
    assert b"Audit activity needs attention" in failed_audit.content
    assert b"agent.draft_failed" in failed_audit.content
    assert reverse("audit").encode() in failed_audit.content


@pytest.mark.django_db
def test_dashboard_aggregates_all_domains_and_opens_recent_mail_in_its_domain(
    client, owner, domain, inbound_message
):
    second = Domain.objects.create(
        owner=owner,
        hostname="second.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PENDING_DNS,
        inbound_ready=False,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    second_conversation = Conversation.objects.create(
        domain=second,
        subject="Second-domain message",
        normalized_subject="second-domain message",
        first_message_at=timezone.now(),
        last_message_at=timezone.now(),
        last_inbound_at=timezone.now(),
    )
    second_message = Message.objects.create(
        domain=second,
        conversation=second_conversation,
        direction=Message.Direction.INBOUND,
        provider_message_id="second-domain-message",
        from_address="sender@outside.example",
        subject="Second-domain message",
        text_body="This belongs to the second domain.",
        received_at=timezone.now(),
        is_quarantined=True,
    )
    MessageRecipient.objects.create(
        domain=domain,
        message=inbound_message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address="requests@example.com",
        is_routing_recipient=True,
    )
    MessageRecipient.objects.create(
        domain=second,
        message=second_message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address="support@second.example",
        is_routing_recipient=True,
    )
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(domain.id)
    session.save()

    response = client.get(reverse("dashboard"))

    assert response.status_code == 200
    assert response.context["metrics"] == {
        "new_messages": 2,
        "quarantined": 1,
        "mailboxes": 2,
    }
    assert [item["domain"].id for item in response.context["domain_readiness_alerts"]] == [
        second.id
    ]
    assert b"across all connected domains" in response.content
    assert domain.hostname.encode() in response.content
    assert second.hostname.encode() in response.content
    assert b'<header class="sticky top-0' in response.content
    assert b"Upgrade to connect domain" not in response.content
    assert b'id="sidebar-toggle"' in response.content
    assert b'id="settings-menu-button"' in response.content
    assert b'id="profile-menu-button"' in response.content
    assert b"Manage plan" in response.content
    assert response.content.index(b'id="settings-menu-button"') > response.content.index(
        b"</aside>"
    )
    detail_url = (
        f"{reverse('conversation_detail', args=[second_conversation.id])}?domain={second.id}"
    )
    assert detail_url.encode() in response.content

    detail = client.get(detail_url)

    assert detail.status_code == 200
    assert client.session["domain_id"] == str(second.id)


@pytest.mark.django_db
def test_conversation_tags_and_personal_api_token_web_actions(
    client, owner, organization, project, conversation
):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    tagged = client.post(
        reverse("conversation_tag", args=[conversation.id]),
        {"operation": "add", "tag": "customer-request"},
    )
    assert tagged.status_code == 302
    assert conversation.tags.filter(normalized_name="customer-request").exists()
    create = client.post(reverse("api_tokens"), {})
    assert create.status_code == 302
    token = APIToken.objects.get(owner=owner, revoked_at__isnull=True)
    reveal = client.get(reverse("api_tokens"))
    assert reveal.status_code == 200
    assert b"shown again" in reveal.content
    assert b"plan-scoped operational access" in reveal.content
    assert b'id="new-token"' not in client.get(reverse("api_tokens")).content
    revoke = client.post(reverse("api_token_revoke", args=[token.id]))
    assert revoke.status_code == 302
    token.refresh_from_db()
    assert token.revoked_at is not None


@pytest.mark.django_db
def test_personal_api_token_can_be_created_before_first_domain(client, owner, organization):
    organization.delete()
    client.force_login(owner)

    page = client.get(reverse("api_tokens"))
    created = client.post(reverse("api_tokens"), {})

    assert page.status_code == 200
    assert b"can connect your first domain" in page.content
    assert created.status_code == 302
    assert APIToken.objects.filter(owner=owner, revoked_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_conversation_tags_render_and_only_audit_real_changes(
    client, owner, organization, conversation
):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    detail_url = reverse("conversation_detail", args=[conversation.id])
    tag_url = reverse("conversation_tag", args=[conversation.id])

    detail = client.get(detail_url)

    assert detail.status_code == 200
    assert b'<label for="conversation-tag"' in detail.content
    assert b"Add a free-form tag" in detail.content
    assert b"Conversation state" not in detail.content
    assert b"Start work" not in detail.content

    changed = client.post(
        tag_url,
        {"operation": "add", "tag": "Needs Owner", "next": detail_url},
        follow=True,
    )

    assert changed.status_code == 200
    assert changed.redirect_chain == [(detail_url, 302)]
    assert b"Tag #Needs Owner added." in changed.content
    assert b"#Needs Owner" in changed.content
    audit_events = AuditEvent.objects.filter(
        domain=organization,
        event_type="conversation.tag_added",
    )
    assert audit_events.count() == 1

    unchanged = client.post(
        tag_url,
        {"operation": "add", "tag": "needs   owner", "next": detail_url},
        follow=True,
    )

    assert unchanged.status_code == 200
    assert b"Tag #Needs Owner added." not in unchanged.content
    assert audit_events.count() == 1


@pytest.mark.django_db
def test_attachment_web_locked_and_expired_responses(client, owner, organization, inbound_message):
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    attachment = Attachment.objects.create(
        domain=organization,
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
def test_owner_can_switch_domains(client, owner, organization, project):
    second = Domain.objects.create(
        owner=owner,
        hostname="second.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        claim_expires_at=timezone.now(),
    )
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    response = client.post(
        reverse("domain_switch"),
        {"domain_id": str(second.id), "next": reverse("dashboard")},
    )
    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session["domain_id"] == str(second.id)


@pytest.mark.django_db
def test_complete_inbox_has_filter_preserving_pagination(
    client, owner, organization, project, conversation
):
    now = timezone.now()
    for index in range(50):
        item = Conversation.objects.create(
            domain=project,
            subject=f"Paged conversation {index:02d}",
            normalized_subject=f"paged conversation {index:02d}",
            first_message_at=now - timedelta(minutes=index + 1),
            last_message_at=now - timedelta(minutes=index + 1),
        )
        item.tags.create(
            domain=project,
            name="agent-review",
            normalized_name="agent-review",
        )
    conversation.tags.create(
        domain=project,
        name="agent-review",
        normalized_name="agent-review",
    )
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    first = client.get(reverse("inbox"), {"tag": "agent-review"})
    second = client.get(reverse("inbox"), {"tag": "agent-review", "page": 2})
    assert first.status_code == 200 and len(first.context["conversations"]) == 50
    assert second.status_code == 200 and len(second.context["conversations"]) == 1
    assert b"tag=agent-review&amp;page=2" in first.content


@pytest.mark.django_db
def test_quarantined_inbox_preview_never_leaks_body(
    client, owner, organization, conversation, inbound_message
):
    inbound_message.is_quarantined = True
    inbound_message.text_body = "DO-NOT-LEAK-QUARANTINED-CONTENT"
    inbound_message.save(update_fields=("is_quarantined", "text_body", "updated_at"))
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()
    response = client.get(reverse("inbox"))
    assert b"DO-NOT-LEAK-QUARANTINED-CONTENT" not in response.content
    assert b"Content locked by the malware quarantine" in response.content

    detail = client.get(reverse("conversation_detail", args=[conversation.id]))
    assert b"This message did not pass the malware scan" in detail.content
    assert b"Quarantined content" in detail.content
    assert b"Conversation state" not in detail.content
    assert b"SES virus verdict" not in detail.content


@pytest.mark.django_db
def test_provider_backed_audit_events_use_product_language(client, owner, organization):
    AuditEvent.objects.create(
        domain=organization,
        actor_type=AuditEvent.ActorType.AWS,
        event_type="domain.ses_identity_adoption_pending",
        object_type="Domain",
        request_id="provider-backed-event",
    )
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(organization.id)
    session.save()

    response = client.get(reverse("audit"))

    assert response.status_code == 200
    assert b"Operational Inbox" in response.content
    assert b"domain.email_configuration_review_started" in response.content
    assert b"domain.ses_identity_adoption_pending" not in response.content
    assert b">AWS<" not in response.content
    assert b"AWS keys" not in response.content
    assert b"S3 keys" not in response.content
