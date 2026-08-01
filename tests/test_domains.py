from __future__ import annotations

import importlib
from datetime import timedelta
from unittest.mock import Mock, patch

import dns.resolver
import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from inbox.management.commands.check_domain_drift import values_match
from inbox.models import (
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DurableJob,
    Organization,
    Project,
)
from inbox.services.domains import (
    application_ownership_record_name,
    apply_domain_readiness,
    build_dns_instructions,
    create_domain,
    create_domain_test,
    inspect_mx,
    normalize_hostname,
    provision_ses_identity,
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
        organization=organization,
        project=project,
        hostname="example.org",
        setup_mode=Domain.SetupMode.DIRECT_MX,
    )
    assert domain.setup_mode == Domain.SetupMode.DIRECT_MX
    assert recommended_setup(domain) == Domain.SetupMode.PROVIDER_FORWARD
    assert domain.existing_mx[0]["exchange"] == "mx1.privateemail.com"


@pytest.mark.django_db
@override_settings(MAX_DOMAINS_PER_ORGANIZATION=1)
def test_domain_limit_is_enforced(monkeypatch, organization, project):
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    create_domain(
        organization=organization,
        project=project,
        hostname="one.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
    )
    with pytest.raises(ValidationError, match="at most 1"):
        create_domain(
            organization=organization,
            project=project,
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
    common = {
        "organization": organization,
        "project": project,
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
    organization = Organization.objects.create(owner=owner, name="Large", slug="large")
    project = Project.objects.create(organization=organization, name="All", slug="all")
    expiry = timezone.now() + timedelta(days=1)
    Domain.objects.bulk_create(
        [
            Domain(
                organization=organization,
                project=project,
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
def test_dns_drift_retries_receipt_rule_after_transient_failure():
    with patch(
        "inbox.management.commands.check_domain_drift.reconcile_receipt_rule",
        side_effect=[RuntimeError("temporary AWS failure"), None],
    ) as reconcile:
        with pytest.raises(RuntimeError, match="temporary AWS failure"):
            call_command("check_domain_drift")
        call_command("check_domain_drift")
    assert reconcile.call_count == 2


@pytest.mark.django_db
def test_existing_ses_identity_requires_a_fresh_application_ownership_proof(organization, project):
    expiry = timezone.now() + timedelta(days=1)
    domain = Domain.objects.create(
        organization=organization,
        project=project,
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
    provision_ses_identity(domain, ses_client=ses)
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
    assert domain.outbound_ready
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTED
    assert AuditEvent.objects.filter(
        event_type="domain.ses_identity_adopted", object_id=domain.id
    ).exists()


@pytest.mark.django_db
def test_failed_existing_ses_identity_restarts_only_after_fresh_proof(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
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

    provision_ses_identity(domain, ses_client=ses)
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

    assert statuses == ("PENDING", "PENDING")
    ses.verify_domain_identity.assert_called_once_with(Domain=domain.hostname)
    ses.verify_domain_dkim.assert_called_once_with(Domain=domain.hostname)
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ses_verification.value == "new-ses-token"
    assert ses_verification.status == DomainDNSRecord.Status.PENDING
    assert set(
        domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).values_list(
            "name", flat=True
        )
    ) == {f"new-{suffix}._domainkey.{domain.hostname}" for suffix in ("one", "two", "three")}
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
    domain.dns_records.filter(
        purpose__in=[
            DomainDNSRecord.Purpose.SES_VERIFICATION,
            DomainDNSRecord.Purpose.DKIM,
        ]
    ).delete()
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
    ses.verify_domain_dkim.assert_called_once_with(Domain=domain.hostname)


@pytest.mark.django_db
def test_new_ses_identity_records_managed_origin_and_separate_dns_proofs(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
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

    provision_ses_identity(domain, ses_client=ses)
    domain.refresh_from_db()

    assert domain.status == Domain.Status.PENDING_DNS
    assert domain.ses_identity_status == "PENDING"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED
    ownership = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.OWNERSHIP)
    ses_verification = domain.dns_records.get(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION)
    assert ownership.name == application_ownership_record_name(domain)
    assert ownership.value != ses_verification.value
    assert ses_verification.value == "ses-proof"

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
def test_direct_receiving_waits_for_ses_identity_success(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
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
def test_existing_identity_recovery_migration_is_idempotent(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="legacy-collision.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.ERROR,
        error_code="ses_identity_collision",
        error_message="Manual review required.",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    migration = importlib.import_module("inbox.migrations.0006_recover_existing_ses_identities")

    migration.prepare_existing_identities(apps, None)
    migration.prepare_existing_identities(apps, None)
    domain.refresh_from_db()

    assert domain.status == Domain.Status.PROVISIONING
    assert domain.error_code == ""
    assert (
        DurableJob.objects.filter(
            idempotency_key=f"provision-domain:{domain.id}:existing-identity-recovery"
        ).count()
        == 1
    )
    assert (
        AuditEvent.objects.filter(
            event_type="domain.provision_recovery_scheduled",
            object_id=domain.id,
            request_id="migration:0006",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_provisioning_cannot_resurrect_a_domain_disabled_during_aws_calls(organization, project):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="disabled-during-provision.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {}}
    ses.get_identity_dkim_attributes.return_value = {"DkimAttributes": {}}
    ses.verify_domain_identity.return_value = {"VerificationToken": "proof"}

    def disable_before_dkim_finishes(**kwargs):
        Domain.objects.filter(id=domain.id).update(status=Domain.Status.DISABLED)
        return {"DkimTokens": ["one", "two", "three"]}

    ses.verify_domain_dkim.side_effect = disable_before_dkim_finishes

    result = provision_ses_identity(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == Domain.Status.DISABLED
    assert domain.status == Domain.Status.DISABLED
    assert domain.ses_identity_status == "PROVISIONING"
    assert domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED
    assert not domain.dns_records.exists()


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
        organization=organization,
        project=project,
        hostname=f"replayed-{status.casefold().replace('_', '-')}.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=status,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()

    result = provision_ses_identity(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == status
    assert domain.status == status
    ses.get_identity_verification_attributes.assert_not_called()
