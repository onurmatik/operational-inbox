from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

import dns.resolver
import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from inbox.models import Domain, Organization, Project
from inbox.services.domains import (
    create_domain,
    inspect_mx,
    normalize_hostname,
    provision_ses_identity,
    recommended_setup,
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
def test_reclaimed_domain_reuses_only_a_previously_managed_ses_identity(organization, project):
    expiry = timezone.now() + timedelta(days=1)
    Domain.objects.create(
        organization=organization,
        project=project,
        hostname="reclaimed.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.DISABLED,
        ses_identity_status="MANAGED",
        claim_expires_at=expiry,
    )
    reclaimed = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="reclaimed.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=expiry,
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {"reclaimed.example": {"VerificationStatus": "Pending"}}
    }
    ses.verify_domain_identity.return_value = {"VerificationToken": "proof"}
    ses.verify_domain_dkim.return_value = {"DkimTokens": ["one", "two", "three"]}
    provision_ses_identity(reclaimed, ses_client=ses)
    reclaimed.refresh_from_db()
    assert reclaimed.status == Domain.Status.PENDING_DNS
    assert reclaimed.ses_identity_status == "MANAGED"

    foreign = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="foreign.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=expiry,
    )
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {"foreign.example": {"VerificationStatus": "Success"}}
    }
    provision_ses_identity(foreign, ses_client=ses)
    foreign.refresh_from_db()
    assert foreign.status == Domain.Status.ERROR
    assert foreign.error_code == "ses_identity_collision"


@pytest.mark.django_db
def test_provisioning_cannot_resurrect_a_domain_disabled_during_aws_calls(
    organization, project
):
    domain = Domain.objects.create(
        organization=organization,
        project=project,
        hostname="disabled-during-provision.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.PROVISIONING,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    ses = Mock()
    ses.get_identity_verification_attributes.return_value = {
        "VerificationAttributes": {}
    }
    ses.verify_domain_identity.return_value = {"VerificationToken": "proof"}

    def disable_before_dkim_finishes(**kwargs):
        Domain.objects.filter(id=domain.id).update(status=Domain.Status.DISABLED)
        return {"DkimTokens": ["one", "two", "three"]}

    ses.verify_domain_dkim.side_effect = disable_before_dkim_finishes

    result = provision_ses_identity(domain, ses_client=ses)

    domain.refresh_from_db()
    assert result.status == Domain.Status.DISABLED
    assert domain.status == Domain.Status.DISABLED
    assert domain.ses_identity_status == "MANAGED"
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
def test_replayed_provisioning_never_regresses_an_advanced_domain(
    organization, project, status
):
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
