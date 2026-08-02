from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time

from inbox.models import (
    AuditEvent,
    Conversation,
    Domain,
    DomainDNSRecord,
    DomainTest,
    DurableJob,
    InboundRoute,
    InboundRoutingTransition,
    Message,
)
from inbox.services.domains import expire_unverified_claims
from inbox.services.ingestion import _route_domains
from inbox.services.jobs import _surface_job_failure
from inbox.services.receipt_rules import receipt_allowlist
from inbox.services.routing_transitions import (
    begin_routing_transition,
    cancel_routing_transition,
    complete_expired_routing_transition,
    create_routing_transition_test,
    ensure_routing_transition_test,
    finalize_routing_transition_test,
    provision_routing_transition,
    refresh_routing_transition,
)

NON_DISABLED_DOMAIN_STATUSES = [
    Domain.Status.PROVISIONING,
    Domain.Status.PENDING_DNS,
    Domain.Status.PENDING_TEST,
    Domain.Status.READY,
    Domain.Status.ERROR,
    Domain.Status.DEGRADED,
]


class _Resolver:
    def __init__(self, *mx_records: tuple[int, str]) -> None:
        self.mx_records = mx_records

    def resolve(self, _hostname: str, record_type: str, *, lifetime: int):
        assert record_type == "MX"
        assert lifetime == 5
        return [
            SimpleNamespace(preference=preference, exchange=exchange)
            for preference, exchange in self.mx_records
        ]


def _route_kind(setup_mode: str) -> str:
    return (
        InboundRoute.Kind.DIRECT_DOMAIN
        if setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )


def _routing_domain(
    project: Domain,
    *,
    hostname: str,
    setup_mode: str,
    status: str = Domain.Status.READY,
) -> tuple[Domain, InboundRoute]:
    domain = Domain.objects.create(
        owner=project.owner,
        hostname=hostname,
        setup_mode=setup_mode,
        status=status,
        ownership_verified=True,
        inbound_ready=status in {Domain.Status.READY, Domain.Status.DEGRADED},
        outbound_ready=True,
        outbound_status=Domain.OutboundStatus.READY,
        outbound_error_code="preserved-outbound-code",
        outbound_error_message="Preserve this outbound diagnostic.",
        ses_identity_status="SUCCESS",
        ses_identity_origin=Domain.SESIdentityOrigin.ADOPTED,
        claim_expires_at=timezone.now() + timedelta(days=3),
        verified_at=timezone.now(),
    )
    local_part = f"route-{domain.id.hex[:20]}"
    route = InboundRoute.objects.create(
        domain=domain,
        kind=_route_kind(setup_mode),
        local_part=local_part,
        address=f"{local_part}@inbound.example.net",
        setup_generation=domain.inbound_setup_generation,
    )
    return domain, route


def _domain_transition_snapshot(domain: Domain) -> tuple[object, ...]:
    return (
        domain.setup_mode,
        domain.status,
        domain.inbound_setup_generation,
        domain.outbound_ready,
        domain.outbound_status,
        domain.outbound_error_code,
        domain.outbound_error_message,
        domain.ses_identity_status,
        domain.ses_identity_origin,
    )


def _target_route(transition: InboundRoutingTransition) -> InboundRoute:
    return transition.routes.get(setup_generation=transition.generation)


def _advance_to_waiting_test(
    transition: InboundRoutingTransition,
) -> InboundRoutingTransition:
    InboundRoutingTransition.objects.filter(id=transition.id).update(
        status=InboundRoutingTransition.Status.WAITING_TEST,
    )
    transition.refresh_from_db()
    return transition


def _inbound_message(domain: Domain, *, provider_message_id: str) -> Message:
    now = timezone.now()
    conversation = Conversation.objects.create(
        domain=domain,
        subject="Routing transition test",
        normalized_subject="routing transition test",
        first_message_at=now,
        last_message_at=now,
        last_inbound_at=now,
    )
    return Message.objects.create(
        domain=domain,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        provider_message_id=provider_message_id,
        from_address="sender@example.net",
        subject="Routing transition test",
        text_body="Exercise the candidate receiving path.",
        received_at=now,
        spam_verdict=Message.Verdict.PASS,
        virus_verdict=Message.Verdict.PASS,
        dkim_verdict=Message.Verdict.PASS,
        spf_verdict=Message.Verdict.PASS,
        dmarc_verdict=Message.Verdict.PASS,
    )


def _begin_waiting_test(
    domain: Domain,
    target_mode: str,
) -> tuple[InboundRoutingTransition, InboundRoute]:
    transition, started = begin_routing_transition(domain, target_mode)
    assert started is True
    transition = _advance_to_waiting_test(transition)
    return transition, _target_route(transition)


@pytest.mark.django_db
def test_provider_to_direct_begin_is_make_before_break(project):
    domain, provider_alias = _routing_domain(
        project,
        hostname="provider-to-direct.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    before = _domain_transition_snapshot(domain)

    transition, started = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)

    domain.refresh_from_db()
    provider_alias.refresh_from_db()
    target_route = _target_route(transition)
    assert started is True
    assert _domain_transition_snapshot(domain) == before
    assert provider_alias.is_active
    assert provider_alias.grace_until is None
    assert target_route.kind == InboundRoute.Kind.DIRECT_DOMAIN
    assert target_route.is_active
    assert target_route.routing_transition_id == transition.id
    assert target_route.setup_generation == transition.generation
    assert transition.from_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert transition.to_mode == Domain.SetupMode.DIRECT_MX
    assert DurableJob.objects.filter(
        kind="provision_routing_transition",
        payload__transition_id=str(transition.id),
    ).exists()


@pytest.mark.django_db
@override_settings(
    AWS_REGION="us-east-1",
    AWS_INGRESS_BUCKET="ingress-bucket",
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:1:inbound",
    INBOUND_SERVICE_DOMAIN="inbound.example.net",
)
def test_direct_target_provisioning_is_staged_without_changing_active_route(project):
    domain, source_route = _routing_domain(
        project,
        hostname="stage-direct-target.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    before = _domain_transition_snapshot(domain)
    transition, _ = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "ses-proof"}
    reconcile = Mock()

    provision_routing_transition(
        transition,
        ses_client=ses,
        receipt_rule_reconciler=reconcile,
    )

    transition.refresh_from_db()
    domain.refresh_from_db()
    source_route.refresh_from_db()
    assert transition.status == InboundRoutingTransition.Status.WAITING_DNS
    assert _domain_transition_snapshot(domain)[:7] == before[:7]
    assert source_route.is_active
    assert set(domain.dns_records.values_list("purpose", flat=True)) == {
        DomainDNSRecord.Purpose.OWNERSHIP,
        DomainDNSRecord.Purpose.SES_VERIFICATION,
        DomainDNSRecord.Purpose.MX,
    }
    reconcile.assert_called_once_with()


@pytest.mark.django_db
@override_settings(
    AWS_REGION="us-east-1",
    AWS_INGRESS_BUCKET="ingress-bucket",
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:1:inbound",
    INBOUND_SERVICE_DOMAIN="inbound.example.net",
)
def test_direct_target_refresh_claims_error_domain_and_enables_receipt_path(project):
    domain, _ = _routing_domain(
        project,
        hostname="recover-direct-target.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.ERROR,
    )
    domain.ownership_verified = False
    domain.verified_at = None
    domain.save(update_fields=("ownership_verified", "verified_at", "updated_at"))
    transition, _ = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "ses-proof"}
    provision_routing_transition(
        transition,
        ses_client=ses,
        receipt_rule_reconciler=lambda: None,
    )
    domain.dns_records.filter(
        purpose__in=(DomainDNSRecord.Purpose.OWNERSHIP, DomainDNSRecord.Purpose.MX)
    ).update(status=DomainDNSRecord.Status.VALID)

    refreshed = refresh_routing_transition(
        transition,
        resolver=_Resolver((10, "inbound-smtp.us-east-1.amazonaws.com")),
        ses_verification_status="Success",
    )

    domain.refresh_from_db()
    assert refreshed.status == InboundRoutingTransition.Status.WAITING_TEST
    assert domain.status == Domain.Status.ERROR
    assert domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert domain.ownership_verified
    assert "recover-direct-target.example" in receipt_allowlist()
    assert [
        item.domain.id for item in _route_domains(["test-any@recover-direct-target.example"])
    ] == [domain.id]


@pytest.mark.django_db
@override_settings(AWS_REGION="us-east-1", INBOUND_SERVICE_DOMAIN="inbound.example.net")
def test_direct_target_rejects_mixed_mx_even_when_expected_record_is_valid(project):
    domain, _ = _routing_domain(
        project,
        hostname="mixed-direct-target.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    transition, _ = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "ses-proof"}
    provision_routing_transition(
        transition,
        ses_client=ses,
        receipt_rule_reconciler=lambda: None,
    )
    domain.dns_records.filter(
        purpose__in=(DomainDNSRecord.Purpose.OWNERSHIP, DomainDNSRecord.Purpose.MX)
    ).update(status=DomainDNSRecord.Status.VALID)

    refreshed = refresh_routing_transition(
        transition,
        resolver=_Resolver(
            (10, "inbound-smtp.us-east-1.amazonaws.com"),
            (20, "mx.provider.example"),
        ),
        ses_verification_status="Success",
    )

    assert refreshed.status == InboundRoutingTransition.Status.WAITING_DNS


@pytest.mark.django_db
def test_direct_to_provider_begin_is_make_before_break(project):
    domain, direct_route = _routing_domain(
        project,
        hostname="direct-to-provider.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    before = _domain_transition_snapshot(domain)

    transition, started = begin_routing_transition(domain, Domain.SetupMode.PROVIDER_FORWARD)

    domain.refresh_from_db()
    direct_route.refresh_from_db()
    target_route = _target_route(transition)
    assert started is True
    assert _domain_transition_snapshot(domain) == before
    assert direct_route.is_active
    assert direct_route.grace_until is None
    assert target_route.kind == InboundRoute.Kind.FORWARDING_ALIAS
    assert target_route.is_active
    assert target_route.routing_transition_id == transition.id
    assert target_route.setup_generation == transition.generation
    assert transition.from_mode == Domain.SetupMode.DIRECT_MX
    assert transition.to_mode == Domain.SetupMode.PROVIDER_FORWARD


@pytest.mark.django_db
@pytest.mark.parametrize("status", NON_DISABLED_DOMAIN_STATUSES)
def test_every_non_disabled_domain_status_can_begin_a_transition(project, status):
    hostname_status = status.casefold().replace("_", "-")
    domain, active_route = _routing_domain(
        project,
        hostname=f"transition-from-{hostname_status}.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=status,
    )

    transition, started = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)

    domain.refresh_from_db()
    active_route.refresh_from_db()
    assert started is True
    assert transition.from_domain_status == status
    assert domain.status == status
    assert domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert active_route.is_active


@pytest.mark.django_db
def test_disabled_domain_cannot_begin_a_transition(project):
    domain, _ = _routing_domain(
        project,
        hostname="disabled-transition.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.DISABLED,
    )

    with pytest.raises(ValidationError, match="disabled"):
        begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)

    assert not InboundRoutingTransition.objects.filter(domain=domain).exists()


@pytest.mark.django_db
def test_duplicate_target_transition_request_is_idempotent(project):
    domain, _ = _routing_domain(
        project,
        hostname="idempotent-transition.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )

    first, first_started = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    second, second_started = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)

    assert first_started is True
    assert second_started is False
    assert second.id == first.id
    assert second.generation == first.generation
    assert (
        InboundRoutingTransition.objects.filter(
            domain=domain,
            to_mode=Domain.SetupMode.DIRECT_MX,
        ).count()
        == 1
    )
    assert second.routes.filter(setup_generation=second.generation, is_active=True).count() == 1


@pytest.mark.django_db
def test_begin_creates_the_next_generation_without_advancing_active_domain(project):
    domain, source_route = _routing_domain(
        project,
        hostname="candidate-generation.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    source_generation = domain.inbound_setup_generation

    transition, _ = begin_routing_transition(domain, Domain.SetupMode.PROVIDER_FORWARD)

    domain.refresh_from_db()
    source_route.refresh_from_db()
    target_route = _target_route(transition)
    assert transition.generation == source_generation + 1
    assert domain.inbound_setup_generation == source_generation
    assert source_route.setup_generation == source_generation
    assert source_route.is_active
    assert target_route.setup_generation == transition.generation
    assert target_route.kind == InboundRoute.Kind.FORWARDING_ALIAS
    assert target_route.is_active


@pytest.mark.django_db
def test_cancel_deactivates_only_the_candidate_route(project):
    domain, source_route = _routing_domain(
        project,
        hostname="cancel-transition.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.DEGRADED,
    )
    before = _domain_transition_snapshot(domain)
    transition, _ = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    target_route = _target_route(transition)

    cancel_routing_transition(transition)

    domain.refresh_from_db()
    source_route.refresh_from_db()
    target_route.refresh_from_db()
    transition.refresh_from_db()
    assert transition.status == InboundRoutingTransition.Status.CANCELLED
    assert _domain_transition_snapshot(domain) == before
    assert source_route.is_active
    assert source_route.grace_until is None
    assert not target_route.is_active


@pytest.mark.django_db
def test_stale_transition_job_failure_cannot_resurrect_a_cancelled_transition(project):
    domain, _ = _routing_domain(
        project,
        hostname="cancelled-job-race.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    transition, _ = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)
    job = DurableJob.objects.get(
        kind="provision_routing_transition",
        payload__transition_id=str(transition.id),
    )
    assert cancel_routing_transition(transition)

    _surface_job_failure(job, terminal=True)

    transition.refresh_from_db()
    assert transition.status == InboundRoutingTransition.Status.CANCELLED


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("source_mode", "target_mode", "expected_kind"),
    [
        (
            Domain.SetupMode.PROVIDER_FORWARD,
            Domain.SetupMode.DIRECT_MX,
            InboundRoute.Kind.DIRECT_DOMAIN,
        ),
        (
            Domain.SetupMode.DIRECT_MX,
            Domain.SetupMode.PROVIDER_FORWARD,
            InboundRoute.Kind.FORWARDING_ALIAS,
        ),
    ],
)
def test_transition_test_is_bound_to_candidate_generation_and_path(
    project,
    source_mode,
    target_mode,
    expected_kind,
):
    domain, _ = _routing_domain(
        project,
        hostname=f"test-binding-{target_mode.casefold().replace('_', '-')}.example",
        setup_mode=source_mode,
    )
    transition, _ = _begin_waiting_test(domain, target_mode)
    reconciler = Mock()

    test, address, created = ensure_routing_transition_test(
        transition,
        receipt_rule_reconciler=reconciler,
    )

    assert test.status == DomainTest.Status.PENDING
    assert test.routing_transition_id == transition.id
    assert test.setup_generation == transition.generation
    assert test.expected_setup_mode == target_mode
    assert test.expected_route_kind == expected_kind
    assert test.address == address
    assert created is True
    assert address.startswith("test-")
    assert address.endswith(f"@{domain.hostname}")

    reused, reused_address, reused_created = ensure_routing_transition_test(
        transition,
        receipt_rule_reconciler=reconciler,
    )
    wrapped, wrapped_address = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=reconciler,
    )

    assert reused.id == wrapped.id == test.id
    assert reused_address == wrapped_address == address
    assert reused_created is False
    assert transition.tests.filter(status=DomainTest.Status.PENDING).count() == 1
    assert reconciler.call_count == (1 if target_mode == Domain.SetupMode.DIRECT_MX else 0)


@pytest.mark.django_db
def test_wrong_arrival_kind_cannot_cut_over(project):
    domain, source_route = _routing_domain(
        project,
        hostname="wrong-arrival-kind.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    before = _domain_transition_snapshot(domain)
    transition, target_route = _begin_waiting_test(domain, Domain.SetupMode.DIRECT_MX)
    test, _ = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=lambda: None,
    )
    message = _inbound_message(domain, provider_message_id="wrong-arrival-kind")

    finalized = finalize_routing_transition_test(
        test,
        message,
        InboundRoute.Kind.FORWARDING_ALIAS,
    )

    domain.refresh_from_db()
    source_route.refresh_from_db()
    target_route.refresh_from_db()
    transition.refresh_from_db()
    test.refresh_from_db()
    assert finalized is False
    assert _domain_transition_snapshot(domain) == before
    assert transition.status == InboundRoutingTransition.Status.WAITING_TEST
    assert transition.grace_until is None
    assert test.status == DomainTest.Status.PENDING
    assert test.received_message_id is None
    assert source_route.is_active
    assert target_route.is_active


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("source_mode", "target_mode", "arrival_kind"),
    [
        (
            Domain.SetupMode.PROVIDER_FORWARD,
            Domain.SetupMode.DIRECT_MX,
            InboundRoute.Kind.DIRECT_DOMAIN,
        ),
        (
            Domain.SetupMode.DIRECT_MX,
            Domain.SetupMode.PROVIDER_FORWARD,
            InboundRoute.Kind.FORWARDING_ALIAS,
        ),
    ],
)
def test_correct_transition_test_atomically_cuts_over_into_grace(
    project,
    source_mode,
    target_mode,
    arrival_kind,
):
    domain, source_route = _routing_domain(
        project,
        hostname=f"cutover-{target_mode.casefold().replace('_', '-')}.example",
        setup_mode=source_mode,
    )
    outbound_before = (
        domain.outbound_ready,
        domain.outbound_status,
        domain.outbound_error_code,
        domain.outbound_error_message,
        domain.ses_identity_status,
        domain.ses_identity_origin,
    )
    transition, target_route = _begin_waiting_test(domain, target_mode)
    test, _ = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=lambda: None,
    )

    with freeze_time("2026-08-02 10:00:00"):
        message = _inbound_message(
            domain,
            provider_message_id=f"correct-{target_mode.casefold()}",
        )
        finalized = finalize_routing_transition_test(test, message, arrival_kind)
        expected_grace_until = timezone.now() + timedelta(hours=24)

    domain.refresh_from_db()
    source_route.refresh_from_db()
    target_route.refresh_from_db()
    transition.refresh_from_db()
    test.refresh_from_db()
    assert finalized is True
    assert domain.setup_mode == target_mode
    assert domain.inbound_setup_generation == transition.generation
    assert (
        domain.outbound_ready,
        domain.outbound_status,
        domain.outbound_error_code,
        domain.outbound_error_message,
        domain.ses_identity_status,
        domain.ses_identity_origin,
    ) == outbound_before
    assert transition.status == InboundRoutingTransition.Status.GRACE
    assert transition.grace_until == expected_grace_until
    assert test.status == DomainTest.Status.RECEIVED
    assert test.received_message_id == message.id
    assert target_route.is_active
    assert source_route.is_active
    assert source_route.grace_until == expected_grace_until


@pytest.mark.django_db
@override_settings(INBOUND_SERVICE_DOMAIN="inbound.example.net")
def test_direct_source_is_accepted_only_until_grace_deadline_even_if_cleanup_is_late(project):
    domain, _ = _routing_domain(
        project,
        hostname="direct-grace-deadline.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    transition, _ = _begin_waiting_test(domain, Domain.SetupMode.PROVIDER_FORWARD)
    test, _ = create_routing_transition_test(transition)

    with freeze_time("2026-08-02 10:00:00"):
        message = _inbound_message(domain, provider_message_id="direct-grace-deadline")
        assert finalize_routing_transition_test(
            test,
            message,
            InboundRoute.Kind.FORWARDING_ALIAS,
        )

    with freeze_time("2026-08-03 09:59:59"):
        assert "direct-grace-deadline.example" in receipt_allowlist()
        assert _route_domains(["anything@direct-grace-deadline.example"])

    with freeze_time("2026-08-03 10:00:01"):
        assert "direct-grace-deadline.example" not in receipt_allowlist()
        assert not _route_domains(["anything@direct-grace-deadline.example"])

    transition.refresh_from_db()
    assert transition.status == InboundRoutingTransition.Status.GRACE


@pytest.mark.django_db
def test_grace_can_start_a_fresh_staged_transition_back(project):
    domain, original_direct_route = _routing_domain(
        project,
        hostname="reverse-during-grace.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    first, current_provider_route = _begin_waiting_test(
        domain,
        Domain.SetupMode.PROVIDER_FORWARD,
    )
    test, _ = create_routing_transition_test(first)
    message = _inbound_message(domain, provider_message_id="reverse-during-grace")
    assert finalize_routing_transition_test(
        test,
        message,
        InboundRoute.Kind.FORWARDING_ALIAS,
    )

    reverse, started = begin_routing_transition(domain, Domain.SetupMode.DIRECT_MX)

    domain.refresh_from_db()
    first.refresh_from_db()
    original_direct_route.refresh_from_db()
    current_provider_route.refresh_from_db()
    assert started
    assert first.status == InboundRoutingTransition.Status.COMPLETE
    assert reverse.from_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert reverse.to_mode == Domain.SetupMode.DIRECT_MX
    assert reverse.generation > first.generation
    assert domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert domain.inbound_setup_generation == first.generation
    assert current_provider_route.is_active
    assert not original_direct_route.is_active
    assert reverse.routes.get().kind == InboundRoute.Kind.DIRECT_DOMAIN
    assert AuditEvent.objects.filter(
        domain=domain,
        event_type="domain.receiving_route_grace_ended_for_reverse",
        object_id=first.id,
    ).exists()


@pytest.mark.django_db
def test_cutover_grants_grace_only_to_the_current_source_generation(project):
    domain, source_route = _routing_domain(
        project,
        hostname="source-generation-grace.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    stale_route = InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="stale-provider-route",
        address="stale-provider-route@inbound.example.net",
        setup_generation=999,
    )
    transition, _ = _begin_waiting_test(domain, Domain.SetupMode.DIRECT_MX)
    test, _ = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=lambda: None,
    )
    message = _inbound_message(domain, provider_message_id="source-generation-grace")

    assert finalize_routing_transition_test(
        test,
        message,
        InboundRoute.Kind.DIRECT_DOMAIN,
    )

    source_route.refresh_from_db()
    stale_route.refresh_from_db()
    assert source_route.grace_until is not None
    assert stale_route.grace_until is None


@pytest.mark.django_db
def test_claim_expiry_cancels_transition_expires_test_and_queues_receipt_cleanup(project):
    domain, _ = _routing_domain(
        project,
        hostname="expired-transition-claim.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_DNS,
    )
    domain.ownership_verified = False
    domain.claim_expires_at = timezone.now() - timedelta(seconds=1)
    domain.save(update_fields=("ownership_verified", "claim_expires_at", "updated_at"))
    transition, _ = _begin_waiting_test(domain, Domain.SetupMode.DIRECT_MX)
    test, _ = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=lambda: None,
    )

    assert expire_unverified_claims() == 1

    domain.refresh_from_db()
    transition.refresh_from_db()
    test.refresh_from_db()
    assert domain.status == Domain.Status.DISABLED
    assert transition.status == InboundRoutingTransition.Status.CANCELLED
    assert test.status == DomainTest.Status.EXPIRED
    assert not domain.inbound_routes.filter(is_active=True).exists()
    assert DurableJob.objects.filter(
        kind="reconcile_receipt_rule",
        idempotency_key=f"receipt-rule:claim-expired:{domain.id}",
    ).exists()


@pytest.mark.django_db
def test_expired_grace_deactivates_the_old_route_and_completes_transition(project):
    domain, source_route = _routing_domain(
        project,
        hostname="complete-routing-grace.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    transition, target_route = _begin_waiting_test(domain, Domain.SetupMode.DIRECT_MX)
    test, _ = create_routing_transition_test(
        transition,
        receipt_rule_reconciler=lambda: None,
    )

    with freeze_time("2026-08-02 10:00:00"):
        message = _inbound_message(domain, provider_message_id="complete-routing-grace")
        assert (
            finalize_routing_transition_test(
                test,
                message,
                InboundRoute.Kind.DIRECT_DOMAIN,
            )
            is True
        )

    with freeze_time("2026-08-03 10:00:01"):
        complete_expired_routing_transition(transition)

    domain.refresh_from_db()
    source_route.refresh_from_db()
    target_route.refresh_from_db()
    transition.refresh_from_db()
    assert domain.setup_mode == Domain.SetupMode.DIRECT_MX
    assert domain.inbound_setup_generation == transition.generation
    assert transition.status == InboundRoutingTransition.Status.COMPLETE
    assert not source_route.is_active
    assert target_route.is_active
