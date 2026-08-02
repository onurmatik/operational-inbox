from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

import dns.resolver
import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from inbox.management.commands.check_domain_drift import values_match
from inbox.models import (
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DomainTest,
    InboundRoute,
    InboundRoutingTransition,
)
from inbox.services.domains import (
    DomainClaimLookupError,
    DomainRoutingClassification,
    MXObservation,
    application_ownership_record_name,
    apply_domain_readiness,
    build_dns_instructions,
    build_inbound_dns_instructions,
    classify_domain_routing,
    classify_mx_layout,
    create_domain,
    create_domain_test,
    expected_inbound_mx_exchange,
    inspect_domain_routing,
    inspect_mx,
    inspect_operational_inbox_claim,
    normalize_hostname,
    provision_inbound,
    provision_outbound_identity,
    recommended_setup,
    reconcile_ses_identity_adoption,
)
from inbox.services.receipt_rules import (
    ReceiptRuleLimitError,
    receipt_allowlist,
    reconcile_receipt_rule,
)


def test_idna_domain_normalization():
    assert normalize_hostname("BÜCHER.example.") == "xn--bcher-kva.example"
    with pytest.raises(ValidationError):
        normalize_hostname("*@example.com")


def test_null_mx_is_not_treated_as_an_existing_mail_provider():
    resolver = Mock()
    resolver.resolve.return_value = [Mock(preference=0, exchange=".")]

    assert inspect_mx("example.org", resolver=resolver) == []


def test_unavailable_nameservers_do_not_look_like_missing_mx():
    resolver = Mock()
    resolver.resolve.side_effect = dns.resolver.NoNameservers()

    with pytest.raises(ValidationError, match="nameservers did not answer"):
        inspect_mx("example.org", resolver=resolver)


@override_settings(AWS_REGION="eu-west-1")
def test_domain_routing_classification_distinguishes_reconnect_external_and_mixed_mx():
    expected = MXObservation(10, "INBOUND-SMTP.EU-WEST-1.AMAZONAWS.COM.")
    external = MXObservation(20, "mx.provider.example")

    reconnect = classify_domain_routing(
        [expected],
        has_operational_inbox_claim=True,
    )
    shared_ses = classify_domain_routing(
        [expected],
        has_operational_inbox_claim=False,
    )
    provider = classify_domain_routing(
        [external],
        has_operational_inbox_claim=True,
    )
    mixed = classify_domain_routing(
        [expected, external],
        has_operational_inbox_claim=True,
    )
    empty = classify_domain_routing([], has_operational_inbox_claim=False)

    assert expected_inbound_mx_exchange() == "inbound-smtp.eu-west-1.amazonaws.com"
    assert reconnect.classification == DomainRoutingClassification.OPERATIONAL_INBOX_RECONNECT
    assert reconnect.recommended_setup_mode == Domain.SetupMode.DIRECT_MX
    assert shared_ses.classification == DomainRoutingClassification.SES_MX_UNCLAIMED
    assert shared_ses.requires_explicit_choice
    assert provider.classification == DomainRoutingClassification.EXTERNAL_MX
    assert provider.recommended_setup_mode == Domain.SetupMode.PROVIDER_FORWARD
    assert mixed.classification == DomainRoutingClassification.MIXED_MX
    assert mixed.requires_explicit_choice
    assert empty.classification == DomainRoutingClassification.NO_MX
    assert empty.recommended_setup_mode == Domain.SetupMode.DIRECT_MX
    assert classify_mx_layout([expected]).value == "OPERATIONAL_INBOX"


def test_domain_routing_inspection_uses_claim_only_as_a_reconnect_hint():
    resolver = Mock()
    mx_answer = Mock(preference=10, exchange="inbound-smtp.us-east-1.amazonaws.com.")
    resolver.resolve.side_effect = [[mx_answer], [Mock()]]

    inspection = inspect_domain_routing("Reconnect.Example.", resolver=resolver)

    assert inspection.classification == DomainRoutingClassification.OPERATIONAL_INBOX_RECONNECT
    assert inspection.has_operational_inbox_claim
    assert resolver.resolve.call_args_list[0].args == ("reconnect.example", "MX")
    assert resolver.resolve.call_args_list[1].args == (
        "_operational-inbox-claim.reconnect.example",
        "TXT",
    )


def test_external_mx_inspection_does_not_depend_on_claim_txt_lookup():
    resolver = Mock()
    resolver.resolve.return_value = [Mock(preference=10, exchange="mx.provider.example.")]

    inspection = inspect_domain_routing("example.org", resolver=resolver)

    assert inspection.classification == DomainRoutingClassification.EXTERNAL_MX
    assert inspection.has_operational_inbox_claim is None
    resolver.resolve.assert_called_once_with("example.org", "MX", lifetime=5)


def test_claim_lookup_absence_and_failure_are_distinct():
    missing = Mock()
    missing.resolve.side_effect = dns.resolver.NoAnswer()
    assert not inspect_operational_inbox_claim("example.org", resolver=missing)

    unavailable = Mock()
    unavailable.resolve.side_effect = dns.exception.Timeout()
    with pytest.raises(DomainClaimLookupError, match="ownership-record lookup timed out"):
        inspect_operational_inbox_claim("example.org", resolver=unavailable)


def test_txt_tokens_are_case_sensitive_but_dns_targets_are_not():
    txt = DomainDNSRecord(record_type="TXT", value="FreshClaimAbC")
    cname = DomainDNSRecord(record_type="CNAME", value="Target.Example.")

    assert values_match(txt, ["FreshClaimAbC"])
    assert not values_match(txt, ["freshclaimabc"])
    assert values_match(cname, ["target.example"])


@pytest.mark.django_db
def test_existing_mx_never_changes_setup_automatically(monkeypatch, organization, project):
    from inbox.services.domains import MXObservation

    monkeypatch.setattr(
        "inbox.services.domains.inspect_mx",
        lambda hostname: [MXObservation(10, "mx1.privateemail.com")],
    )
    domain = create_domain(
        owner=project.owner,
        hostname="example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    assert domain.setup_mode == Domain.SetupMode.DIRECT_MX
    assert recommended_setup(domain) == Domain.SetupMode.PROVIDER_FORWARD
    assert domain.existing_mx[0]["exchange"] == "mx1.privateemail.com"


@pytest.mark.django_db
@override_settings(MAX_DOMAINS_PER_USER=2)
def test_domain_limit_is_enforced(monkeypatch, organization, project):
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    create_domain(
        owner=project.owner,
        hostname="one.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    with pytest.raises(ValidationError, match="at most 2"):
        create_domain(
            owner=project.owner,
            hostname="two.example",
            setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        )


@pytest.mark.django_db
@override_settings(
    INBOUND_SERVICE_DOMAIN="inbound.operationalinbox.com",
    AWS_INGRESS_BUCKET="bucket",
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:1:inbound",
    AWS_SES_RECEIPT_RULE_SET="rules",
    AWS_SES_RECEIPT_RULE="allowlist",
)
def test_receipt_allowlist_only_adopts_verified_direct_domains(organization, project):
    project.setup_mode = Domain.SetupMode.PROVIDER_FORWARD
    project.save(update_fields=("setup_mode", "updated_at"))
    common = {
        "owner": project.owner,
        "claim_expires_at": timezone.now() + timedelta(days=1),
        "status": Domain.Status.PENDING_TEST,
    }
    Domain.objects.create(
        hostname="direct.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        ownership_verified=True,
        **common,
    )
    Domain.objects.create(
        hostname="forward.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        ownership_verified=True,
        **common,
    )
    Domain.objects.create(
        hostname="unverified.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        ownership_verified=False,
        **common,
    )
    Domain.objects.create(
        hostname="failed.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        ownership_verified=True,
        **{**common, "status": Domain.Status.ERROR},
    )
    assert receipt_allowlist() == ("direct.example", "inbound.operationalinbox.com")
    ses = Mock()
    ses.describe_receipt_rule.return_value = {"Rule": {}}
    result = reconcile_receipt_rule(ses)
    assert result.action == "updated"
    recipients = ses.update_receipt_rule.call_args.kwargs["Rule"]["Recipients"]
    assert recipients and "forward.example" not in recipients


@pytest.mark.django_db
@override_settings(INBOUND_SERVICE_DOMAIN="inbound.operationalinbox.com")
def test_receipt_allowlist_rejects_recipient_501(owner):
    expiry = timezone.now() + timedelta(days=1)
    Domain.objects.bulk_create(
        [
            Domain(
                owner=owner,
                hostname=f"d{index}.example",
                setup_mode=Domain.SetupMode.DIRECT_MX,
                ownership_verified=True,
                status=Domain.Status.PENDING_TEST,
                claim_expires_at=expiry,
            )
            for index in range(500)
        ]
    )
    with pytest.raises(ReceiptRuleLimitError):
        receipt_allowlist()


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET="bucket",
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:1:inbound",
)
def test_dns_drift_retries_receipt_rule_after_transient_failure(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="direct-drift.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="proof",
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    with (
        patch(
            "inbox.management.commands.check_domain_drift.reconcile_receipt_rule",
            side_effect=[RuntimeError("temporary AWS failure"), None],
        ) as reconcile,
        patch(
            "inbox.management.commands.check_domain_drift.observed_values",
            return_value=["proof"],
        ),
        patch("inbox.management.commands.check_domain_drift.boto3.client", return_value=ses),
    ):
        with pytest.raises(RuntimeError, match="temporary AWS failure"):
            call_command("check_domain_drift")
        call_command("check_domain_drift")
    assert reconcile.call_count == 2


@pytest.mark.django_db
def test_existing_ses_identity_requires_a_fresh_application_ownership_proof(organization, project):
    expiry = timezone.now() + timedelta(days=1)
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="existing.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=expiry,
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {
            "existing.example": {
                "VerificationStatus": "Success",
                "VerificationToken": "public-existing-ses-token",
            }
        }
    }
    ses.get_identity_dkim_attributes.return_value = {
        "DkimAttributes": {
            "existing.example": {
                "DkimVerificationStatus": "Success",
                "DkimTokens": ["one", "two", "three"],
            }
        }
    }
    provision_inbound(domain, ses_client=ses)
    domain.refresh_from_db()

    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.ses_identity_status == "SUCCESS"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTION_PENDING
    ses.verify_domain_identity.assert_not_called()
    ses.verify_domain_dkim.assert_not_called()
    assert AuditEvent.objects.filter(
        event_type="domain.ses_identity_adoption_pending", object_id=domain.id
    ).exists()

    ownership = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.OWNERSHIP)
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ownership.name == application_ownership_record_name(domain)
    assert ownership.value != "public-existing-ses-token"
    assert ses_verification.value == "public-existing-ses-token"

    # A pre-published SES token and valid routing record are not sufficient to
    # claim the domain: only the new claim-bound nonce counts as ownership.
    ses_verification.status = DomainDNSRecord.Status.VALID
    ses_verification.save(update_fields=("status", "updated_at"))
    domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.MX).update(
        status=DomainDNSRecord.Status.VALID
    )
    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()
    assert not domain.ownership_verified
    assert not domain.outbound_ready
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTION_PENDING

    ownership.status = DomainDNSRecord.Status.VALID
    ownership.save(update_fields=("status", "updated_at"))
    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()
    assert domain.ownership_verified
    assert domain.outbound_status == Domain.OutboundStatus.DISABLED
    assert not domain.outbound_ready
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTED
    assert AuditEvent.objects.filter(
        event_type="domain.ses_identity_adopted", object_id=domain.id
    ).exists()


@pytest.mark.django_db
def test_failed_existing_ses_identity_restarts_only_after_fresh_proof(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="failed-existing.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {
            domain.hostname: {
                "VerificationStatus": "Failed",
                "VerificationToken": "old-ses-token",
            }
        }
    }
    ses.get_identity_dkim_attributes.return_value = {
        "DkimAttributes": {
            domain.hostname: {
                "DkimVerificationStatus": "Failed",
                "DkimTokens": ["old-one", "old-two", "old-three"],
            }
        }
    }
    ses.verify_domain_identity.return_value = {"VerificationToken": "new-ses-token"}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["new-one", "new-two", "new-three"]}

    provision_inbound(domain, ses_client=ses)
    domain.refresh_from_db()
    ownership = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.OWNERSHIP)

    assert (
        reconcile_ses_identity_adoption(
            domain,
            ses_verification_status="Failed",
            dkim_verification_status="Failed",
            ses_client=ses,
        )
        is None
    )
    ses.verify_domain_identity.assert_not_called()
    ses.verify_domain_dkim.assert_not_called()

    ownership.status = DomainDNSRecord.Status.VALID
    ownership.save(update_fields=("status", "updated_at"))
    statuses = reconcile_ses_identity_adoption(
        domain,
        ses_verification_status="Failed",
        dkim_verification_status="Failed",
        ses_client=ses,
    )

    assert statuses == ("PENDING", "FAILED")
    ses.verify_domain_identity.assert_called_once_with(Domain=domain.hostname)
    ses.verify_domain_dkim.assert_not_called()
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ses_verification.value == "new-ses-token"
    assert ses_verification.status == DomainDNSRecord.Status.PENDING
    assert not domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exists()
    assert AuditEvent.objects.filter(
        event_type="domain.ses_identity_reinitialized", object_id=domain.id
    ).exists()

    apply_domain_readiness(
        domain,
        ses_verification_status=statuses[0],
        dkim_verification_status=statuses[1],
    )
    domain.refresh_from_db()
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTED
    assert domain.status == Domain.Status.PENDING_DNS

    # A later retry also repairs an adopted identity whose pending DNS
    # instructions were lost, without requiring another ownership challenge.
    domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION).delete()
    ses.reset_mock()
    ses.verify_domain_identity.return_value = {"VerificationToken": "replacement-ses-token"}
    ses.verify_domain_dkim.return_value = {
        "DkimTokens": ["replacement-one", "replacement-two", "replacement-three"]
    }
    assert reconcile_ses_identity_adoption(
        domain,
        ses_verification_status="Pending",
        dkim_verification_status="Pending",
        ses_client=ses,
    ) == ("PENDING", "PENDING")
    ses.verify_domain_identity.assert_called_once_with(Domain=domain.hostname)
    ses.verify_domain_dkim.assert_not_called()


@pytest.mark.django_db
def test_new_ses_identity_records_managed_origin_and_separate_dns_proofs(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="new-identity.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "ses-proof"}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["one", "two", "three"]}

    provision_inbound(domain, ses_client=ses)
    domain.refresh_from_db()

    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.ses_identity_status == "PENDING"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED
    ownership = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.OWNERSHIP)
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ownership.name == application_ownership_record_name(domain)
    assert ownership.value != ses_verification.value
    assert ses_verification.value == "ses-proof"
    assert not domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exists()
    assert domain.outbound_status == Domain.OutboundStatus.DISABLED
    ses.get_identity_dkim_attributes.assert_not_called()
    ses.verify_domain_dkim.assert_not_called()

    domain.dns_records.filter(is_required=True).update(status=DomainDNSRecord.Status.VALID)
    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()
    assert domain.ses_identity_status == "SUCCESS"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED


@pytest.mark.django_db
def test_provider_forward_inbound_provisioning_does_not_touch_customer_ses(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="forward-only.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()

    result = provision_inbound(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == Domain.Status.PENDING_DNS
    assert domain.outbound_status == Domain.OutboundStatus.DISABLED
    assert domain.ses_identity_status == ""
    assert domain.ses_identity_origin == ""
    assert list(domain.dns_records.values_list("purpose", flat=True)) == [
        DomainDNSRecord.Purpose.OWNERSHIP
    ]
    assert domain.dns_records.get().name == application_ownership_record_name(domain)
    assert not ses.mock_calls


@pytest.mark.django_db
def test_provider_forward_dns_check_does_not_query_customer_ses(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="forward-dns.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="fresh-proof",
    )
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-forward-dns",
        address="route-forward-dns@inbound.example.net",
    )

    with (
        patch(
            "inbox.management.commands.check_domain_drift.observed_values",
            return_value=["fresh-proof"],
        ),
        patch(
            "inbox.management.commands.check_domain_drift.boto3.client",
            side_effect=AssertionError("provider-forward inbound must not query SES"),
        ),
    ):
        call_command("check_domain_drift")
        call_command("check_domain_drift")

    domain.refresh_from_db()
    assert domain.ownership_verified
    assert domain.status == Domain.Status.PENDING_TEST
    assert domain.outbound_status == Domain.OutboundStatus.DISABLED
    test = DomainTest.objects.get(domain=domain, status=DomainTest.Status.PENDING)
    assert test.address
    assert test.address.startswith("test-")
    assert test.address.endswith(f"@{domain.hostname}")
    audit = AuditEvent.objects.get(
        domain=domain,
        event_type="domain.test_created",
        object_id=test.id,
    )
    assert audit.actor_type == AuditEvent.ActorType.SYSTEM
    assert audit.object_type == "DomainTest"
    assert audit.request_id == f"dns:{domain.id}:test:{test.id}"


@pytest.mark.django_db
@override_settings(
    AWS_INGRESS_BUCKET="bucket",
    AWS_INBOUND_TOPIC_ARN="arn:aws:sns:us-east-1:1:inbound",
)
def test_dns_check_auto_creates_and_reuses_the_transition_challenge(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="transition-challenge.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        ownership_verified=True,
        inbound_ready=True,
        ses_identity_status="SUCCESS",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="transition-proof",
    )
    InboundRoute.objects.create(
        domain=domain,
        setup_generation=1,
        kind=InboundRoute.Kind.DIRECT_DOMAIN,
        local_part="transition-source",
        address="transition-source@inbound.example.net",
    )
    transition = InboundRoutingTransition.objects.create(
        domain=domain,
        generation=2,
        from_mode=Domain.SetupMode.DIRECT_MX,
        to_mode=Domain.SetupMode.PROVIDER_FORWARD,
        from_domain_status=Domain.Status.READY,
        status=InboundRoutingTransition.Status.WAITING_DNS,
    )
    InboundRoute.objects.create(
        domain=domain,
        routing_transition=transition,
        setup_generation=transition.generation,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="transition-target",
        address="transition-target@inbound.example.net",
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {
            domain.hostname: {"VerificationStatus": "Success"},
        }
    }

    with (
        patch(
            "inbox.management.commands.check_domain_drift.observed_values",
            return_value=["transition-proof"],
        ),
        patch(
            "inbox.services.routing_transitions.inspect_mx",
            return_value=[MXObservation(10, "mx.provider.example")],
        ),
        patch("inbox.management.commands.check_domain_drift.boto3.client", return_value=ses),
        patch("inbox.management.commands.check_domain_drift.reconcile_receipt_rule"),
    ):
        call_command("check_domain_drift")
        call_command("check_domain_drift")

    transition.refresh_from_db()
    assert transition.status == InboundRoutingTransition.Status.WAITING_TEST
    test = DomainTest.objects.get(
        domain=domain,
        routing_transition=transition,
        status=DomainTest.Status.PENDING,
    )
    assert test.address and test.address.endswith(f"@{domain.hostname}")
    audit = AuditEvent.objects.get(
        domain=domain,
        event_type="domain.routing_transition_test_created",
        object_id=test.id,
    )
    assert audit.actor_type == AuditEvent.ActorType.SYSTEM
    assert audit.object_type == "DomainTest"
    assert audit.request_id == f"dns:{domain.id}:test:{test.id}"
    assert audit.metadata == {
        "routing_transition_id": str(transition.id),
        "setup_generation": transition.generation,
    }


@pytest.mark.django_db
def test_provider_forward_outbound_enable_is_the_first_customer_ses_touch(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="forward-send.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.READY,
        inbound_ready=True,
        ownership_verified=True,
        outbound_status=Domain.OutboundStatus.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="fresh-proof",
        status=DomainDNSRecord.Status.VALID,
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "ses-proof"}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["one", "two", "three"]}

    provision_outbound_identity(domain, ses_client=ses)

    domain.refresh_from_db()
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert domain.outbound_status == Domain.OutboundStatus.PENDING_DNS
    assert not domain.outbound_ready
    assert set(domain.dns_records.values_list("purpose", flat=True)) == {
        DomainDNSRecord.Purpose.OWNERSHIP,
        DomainDNSRecord.Purpose.SES_VERIFICATION,
        DomainDNSRecord.Purpose.DKIM,
    }
    ses.verify_domain_identity.assert_called_once_with(Domain=domain.hostname)
    ses.verify_domain_dkim.assert_called_once_with(Domain=domain.hostname)


@pytest.mark.django_db
def test_direct_outbound_reuses_inbound_identity_and_adds_dkim(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="direct-send.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        inbound_ready=True,
        ownership_verified=True,
        outbound_status=Domain.OutboundStatus.PROVISIONING,
        ses_identity_status="SUCCESS",
        ses_identity_origin=Domain.SESIdentityOrigin.MANAGED,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    build_inbound_dns_instructions(
        domain,
        ownership_token="fresh-proof",
        verification_token="ses-proof",
    )
    domain.dns_records.update(status=DomainDNSRecord.Status.VALID)
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {
            domain.hostname: {
                "VerificationStatus": "Success",
                "VerificationToken": "ses-proof",
            }
        }
    }
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["one", "two", "three"]}

    provision_outbound_identity(domain, ses_client=ses)

    domain.refresh_from_db()
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert domain.outbound_status == Domain.OutboundStatus.PENDING_DNS
    assert domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).count() == 3
    assert domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION).count() == 1
    ses.verify_domain_identity.assert_not_called()
    ses.verify_domain_dkim.assert_called_once_with(Domain=domain.hostname)


@pytest.mark.django_db
def test_direct_receiving_waits_for_ses_identity_success(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="ses-pending.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PENDING_DNS,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    build_dns_instructions(
        domain,
        ownership_token="fresh-claim",
        verification_token="ses-proof",
        dkim_tokens=["one", "two", "three"],
    )
    domain.dns_records.filter(is_required=True).update(status=DomainDNSRecord.Status.VALID)

    apply_domain_readiness(
        domain,
        ses_verification_status="Pending",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()

    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.ownership_verified
    assert not domain.inbound_ready
    assert not domain.outbound_ready
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ses_verification.is_required

    domain.status = Domain.Status.PENDING_TEST
    domain.save(update_fields=("status", "updated_at"))
    reconciler = Mock()
    with pytest.raises(ValidationError, match="Verify the required DNS records"):
        create_domain_test(domain, receipt_rule_reconciler=reconciler)
    reconciler.assert_not_called()

    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )
    domain.refresh_from_db()
    assert domain.status == Domain.Status.PENDING_TEST


@pytest.mark.django_db
def test_provisioning_cannot_resurrect_a_domain_disabled_during_aws_calls(organization, project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="disabled-during-provision.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "proof"}

    def disable_before_identity_finishes(**kwargs):
        Domain.objects.filter(id=domain.id).update(status=Domain.Status.DISABLED)
        return {"VerificationToken": "proof"}

    ses.verify_domain_identity.side_effect = disable_before_identity_finishes

    result = provision_inbound(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == Domain.Status.DISABLED
    assert domain.status == Domain.Status.DISABLED
    assert domain.ses_identity_status == "PROVISIONING"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED
    assert not domain.dns_records.exists()


@pytest.mark.django_db
def test_stale_direct_attempt_does_not_record_ses_intent_after_lifecycle_change(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="stale-before-intent.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()

    def disable_during_identity_read(**kwargs):
        Domain.objects.filter(id=domain.id).update(status=Domain.Status.DISABLED)
        return {"VerificationAttributes": {}}

    ses.get_identity_verification_attributes.side_effect = disable_during_identity_read

    result = provision_inbound(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == Domain.Status.DISABLED
    assert domain.status == Domain.Status.DISABLED
    assert domain.ses_identity_status == ""
    assert domain.ses_identity_origin == ""
    ses.verify_domain_identity.assert_not_called()


@pytest.mark.django_db
def test_readiness_checks_leave_provisioning_generation_untouched(project):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname="provisioning-readiness.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="old-observation",
        status=DomainDNSRecord.Status.VALID,
    )

    assert not apply_domain_readiness(domain)

    domain.refresh_from_db()
    assert domain.status == Domain.Status.PROVISIONING
    assert not domain.ownership_verified
    assert domain.last_checked_at is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        Domain.Status.PENDING_DNS,
        Domain.Status.PENDING_TEST,
        Domain.Status.READY,
        Domain.Status.DEGRADED,
        Domain.Status.ERROR,
    ],
)
def test_replayed_provisioning_never_regresses_an_advanced_domain(organization, project, status):
    domain = Domain.objects.create(
        owner=project.owner,
        hostname=f"replayed-{status.casefold().replace('_', '-')}.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=status,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()

    result = provision_inbound(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == status
    assert domain.status == status
    ses.get_identity_verification_attributes.assert_not_called()


def _ready_direct_capability_domain(
    project, hostname: str
) -> tuple[Domain, DomainDNSRecord, DomainDNSRecord]:
    domain = Domain.objects.create(
        owner=project.owner,
        hostname=hostname,
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        inbound_ready=True,
        outbound_ready=True,
        outbound_status=Domain.OutboundStatus.READY,
        ses_identity_status="SUCCESS",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    InboundRoute.objects.create(
        domain=domain,
        kind=InboundRoute.Kind.DIRECT_DOMAIN,
        local_part=f"route-{hostname}",
        address=f"route-{hostname}@inbound.example.net",
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value="claim",
        status=DomainDNSRecord.Status.VALID,
    )
    DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
        record_type="TXT",
        name=f"_amazonses.{hostname}",
        value="ses-proof",
        status=DomainDNSRecord.Status.VALID,
    )
    mx = DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.MX,
        record_type="MX",
        name=hostname,
        value="inbound-smtp.us-east-1.amazonaws.com",
        priority=10,
        status=DomainDNSRecord.Status.VALID,
    )
    dkim = DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.DKIM,
        record_type="CNAME",
        name=f"one._domainkey.{hostname}",
        value="one.dkim.amazonses.com",
        is_required=False,
        status=DomainDNSRecord.Status.VALID,
    )
    DomainTest.objects.create(
        domain=domain,
        token_hash="d" * 64,
        status=DomainTest.Status.RECEIVED,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return domain, mx, dkim


@pytest.mark.django_db
def test_mx_drift_degrades_only_inbound(project):
    domain, mx, _ = _ready_direct_capability_domain(project, "mx-drift.example")
    mx.status = DomainDNSRecord.Status.MISSING
    mx.save(update_fields=("status", "updated_at"))

    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )

    domain.refresh_from_db()
    assert domain.status == Domain.Status.DEGRADED
    assert not domain.inbound_ready
    assert domain.outbound_status == Domain.OutboundStatus.READY
    assert domain.outbound_ready


@pytest.mark.django_db
def test_dkim_drift_degrades_only_outbound(project):
    domain, _, dkim = _ready_direct_capability_domain(project, "dkim-drift.example")
    dkim.status = DomainDNSRecord.Status.MISSING
    dkim.save(update_fields=("status", "updated_at"))

    apply_domain_readiness(
        domain,
        ses_verification_status="Success",
        dkim_verification_status="Success",
    )

    domain.refresh_from_db()
    assert domain.status == Domain.Status.READY
    assert domain.inbound_ready
    assert domain.outbound_status == Domain.OutboundStatus.DEGRADED
    assert not domain.outbound_ready
    assert domain.outbound_error_code == "outbound_dns_drift"
