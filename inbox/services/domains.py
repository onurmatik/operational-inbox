from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import boto3
import dns.exception
import dns.resolver
import idna
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DomainTest,
    InboundRoute,
    Organization,
    Project,
)


class DomainLimitError(ValidationError):
    pass


class DomainClaimConflict(ValidationError):
    def __init__(self, *, existing_domain: Domain | None = None) -> None:
        self.existing_domain = existing_domain
        super().__init__(
            {"hostname": "This domain already has an active or pending ownership claim."}
        )


@dataclass(frozen=True)
class MXObservation:
    preference: int
    exchange: str


def normalize_hostname(value: str) -> str:
    candidate = value.strip().rstrip(".").casefold()
    if not candidate or "@" in candidate or candidate.startswith("*."):
        raise ValidationError("Enter a domain name without an email address or wildcard.")
    try:
        ascii_name = idna.encode(candidate, uts46=True, std3_rules=True).decode("ascii")
    except idna.IDNAError as exc:
        raise ValidationError("Enter a valid internationalized domain name.") from exc
    if len(ascii_name) > 253 or any(len(label) > 63 for label in ascii_name.split(".")):
        raise ValidationError("The domain name is too long.")
    if "." not in ascii_name:
        raise ValidationError("Enter a registrable domain or subdomain.")
    return ascii_name


def inspect_mx(hostname: str, resolver: dns.resolver.Resolver | None = None) -> list[MXObservation]:
    resolver = resolver or dns.resolver.Resolver()
    try:
        answers = resolver.resolve(hostname, "MX", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.resolver.NoNameservers as exc:
        raise ValidationError(
            "The domain's nameservers did not answer the MX lookup. Try again shortly."
        ) from exc
    except (dns.exception.Timeout, dns.resolver.LifetimeTimeout) as exc:
        raise ValidationError("The MX lookup timed out. Try again shortly.") from exc
    observations = []
    for answer in answers:
        exchange = str(answer.exchange).rstrip(".")
        if not exchange:
            # RFC 7505 null MX (`0 .`) explicitly declares that the domain has
            # no mail service. It is not an existing provider to preserve.
            continue
        observations.append(MXObservation(preference=int(answer.preference), exchange=exchange))
    return sorted(observations, key=lambda item: (item.preference, item.exchange))


def _assert_limits(organization: Organization) -> None:
    if (
        Domain.objects.filter(organization=organization)
        .exclude(status=Domain.Status.DISABLED)
        .count()
        >= settings.MAX_DOMAINS_PER_ORGANIZATION
    ):
        raise DomainLimitError(
            "An organization can provision at most "
            f"{settings.MAX_DOMAINS_PER_ORGANIZATION} domains."
        )
    since = timezone.now() - timedelta(seconds=settings.DOMAIN_PROVISION_RATE_WINDOW_SECONDS)
    recent = Domain.objects.filter(organization=organization, created_at__gte=since).count()
    if recent >= settings.DOMAIN_PROVISION_RATE_LIMIT:
        raise DomainLimitError("Domain provisioning is temporarily rate limited. Try again later.")


def create_domain(
    *, organization: Organization, project: Project, hostname: str, setup_mode: str
) -> Domain:
    if project.organization_id != organization.id:
        raise ValidationError("The project does not belong to this organization.")
    if setup_mode not in Domain.SetupMode.values:
        raise ValidationError({"setup_mode": "Select a supported setup mode."})
    normalized = normalize_hostname(hostname)
    mx = inspect_mx(normalized)
    # DNS may take several seconds on a degraded nameserver. Keep it outside the
    # write transaction so one lookup cannot hold SQLite's immediate write lock.
    existing = (
        Domain.objects.filter(hostname=normalized).exclude(status=Domain.Status.DISABLED).first()
    )
    if existing is not None:
        raise DomainClaimConflict(
            existing_domain=existing if existing.organization_id == organization.id else None
        )
    try:
        with transaction.atomic():
            _assert_limits(organization)
            domain = Domain.objects.create(
                organization=organization,
                project=project,
                hostname=normalized,
                setup_mode=setup_mode,
                status=Domain.Status.PROVISIONING,
                existing_mx=[
                    {"preference": item.preference, "exchange": item.exchange} for item in mx
                ],
                claim_expires_at=timezone.now() + timedelta(hours=settings.DOMAIN_CLAIM_TTL_HOURS),
            )
            local_part = f"route-{secrets.token_urlsafe(24).lower()}"
            InboundRoute.objects.create(
                organization=organization,
                domain=domain,
                kind=(
                    InboundRoute.Kind.FORWARDING_ALIAS
                    if setup_mode == Domain.SetupMode.PROVIDER_FORWARD
                    else InboundRoute.Kind.DIRECT_DOMAIN
                ),
                local_part=local_part,
                address=f"{local_part}@{settings.INBOUND_SERVICE_DOMAIN}",
            )
    except IntegrityError as exc:
        # Resolve the check/insert race against the database's global active
        # hostname constraint. Do not turn an unrelated integrity failure into
        # a claim conflict.
        existing = (
            Domain.objects.filter(hostname=normalized)
            .exclude(status=Domain.Status.DISABLED)
            .first()
        )
        if existing is None:
            raise
        raise DomainClaimConflict(
            existing_domain=existing if existing.organization_id == organization.id else None
        ) from exc
    return domain


def recommended_setup(domain: Domain) -> str:
    if domain.existing_mx:
        return Domain.SetupMode.PROVIDER_FORWARD
    return domain.setup_mode


def application_ownership_record_name(domain: Domain) -> str:
    return f"_operational-inbox-claim.{domain.hostname}"


def _upsert_dns_instruction(
    domain: Domain,
    *,
    purpose: str,
    record_type: str,
    name: str,
    value: str,
    is_required: bool,
    priority: int | None = None,
) -> DomainDNSRecord:
    record, created = DomainDNSRecord.objects.get_or_create(
        organization=domain.organization,
        domain=domain,
        purpose=purpose,
        record_type=record_type,
        name=name,
        defaults={
            "value": value,
            "priority": priority,
            "is_required": is_required,
        },
    )
    if created:
        return record
    if record.value == value and record.priority == priority and record.is_required == is_required:
        return record
    record.value = value
    record.priority = priority
    record.is_required = is_required
    record.status = DomainDNSRecord.Status.PENDING
    record.observed_values = []
    record.last_checked_at = None
    record.error_message = ""
    record.save(
        update_fields=(
            "value",
            "priority",
            "is_required",
            "status",
            "observed_values",
            "last_checked_at",
            "error_message",
            "updated_at",
        )
    )
    return record


def build_dns_instructions(
    domain: Domain,
    *,
    ownership_token: str,
    verification_token: str,
    dkim_tokens: list[str],
) -> None:
    _upsert_dns_instruction(
        domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name=application_ownership_record_name(domain),
        value=ownership_token,
        is_required=True,
    )
    if verification_token:
        _upsert_dns_instruction(
            domain,
            purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
            record_type="TXT",
            name=f"_amazonses.{domain.hostname}",
            value=verification_token,
            is_required=domain.setup_mode == Domain.SetupMode.DIRECT_MX,
        )
    if domain.setup_mode == Domain.SetupMode.DIRECT_MX:
        _upsert_dns_instruction(
            domain,
            purpose=DomainDNSRecord.Purpose.MX,
            record_type="MX",
            name=domain.hostname,
            value="inbound-smtp.us-east-1.amazonaws.com",
            priority=10,
            is_required=True,
        )
    for token in dkim_tokens:
        _upsert_dns_instruction(
            domain,
            purpose=DomainDNSRecord.Purpose.DKIM,
            record_type="CNAME",
            name=f"{token}._domainkey.{domain.hostname}",
            value=f"{token}.dkim.amazonses.com",
            is_required=False,
        )
    if dkim_tokens:
        current_dkim_names = {f"{token}._domainkey.{domain.hostname}" for token in dkim_tokens}
        domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exclude(
            name__in=current_dkim_names
        ).delete()


def expire_unverified_claims() -> int:
    now = timezone.now()
    expired = list(
        Domain.objects.filter(
            ownership_verified=False,
            claim_expires_at__lte=now,
            status__in=[Domain.Status.PROVISIONING, Domain.Status.PENDING_DNS],
        )
    )
    for domain in expired:
        domain.status = Domain.Status.DISABLED
        domain.inbound_ready = False
        domain.outbound_ready = False
        domain.error_code = "claim_expired"
        domain.error_message = "The ownership claim expired before verification."
        domain.save(
            update_fields=(
                "status",
                "inbound_ready",
                "outbound_ready",
                "error_code",
                "error_message",
                "updated_at",
            )
        )
        domain.inbound_routes.update(is_active=False)
    return len(expired)


def provision_ses_identity(domain: Domain, *, ses_client=None) -> Domain:
    if domain.status == Domain.Status.DISABLED:
        return domain
    if domain.status != Domain.Status.PROVISIONING:
        return domain
    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    attributes = client.get_identity_verification_attributes(Identities=[domain.hostname]).get(
        "VerificationAttributes", {}
    )
    identity_attributes = attributes.get(domain.hostname, {})
    dkim_attributes = client.get_identity_dkim_attributes(Identities=[domain.hostname]).get(
        "DkimAttributes", {}
    )
    identity_dkim_attributes = dkim_attributes.get(domain.hostname, {})
    identity_exists = domain.hostname in attributes or domain.hostname in dkim_attributes
    may_manage_identity = domain.ses_identity_origin == Domain.SESIdentityOrigin.MANAGED
    verification_token = str(identity_attributes.get("VerificationToken", ""))
    verification_status = str(identity_attributes.get("VerificationStatus", "")).upper()
    dkim_tokens = [str(value) for value in identity_dkim_attributes.get("DkimTokens", [])]

    if not identity_exists:
        # Persist intent before the AWS calls. A retry can then distinguish an
        # identity this application started creating from an unrelated existing
        # identity without overloading the observed SES verification status.
        domain.ses_identity_status = "PROVISIONING"
        domain.ses_identity_origin = Domain.SESIdentityOrigin.MANAGED
        domain.save(update_fields=("ses_identity_status", "ses_identity_origin", "updated_at"))
        may_manage_identity = True

    if may_manage_identity and (not verification_token or verification_status == "FAILED"):
        verification = client.verify_domain_identity(Domain=domain.hostname)
        verification_token = str(verification["VerificationToken"])
        verification_status = "PENDING"
    if may_manage_identity and (
        not dkim_tokens
        or str(identity_dkim_attributes.get("DkimVerificationStatus", "")).upper() == "FAILED"
    ):
        dkim = client.verify_domain_dkim(Domain=domain.hostname)
        dkim_tokens = [str(value) for value in dkim.get("DkimTokens", [])]

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        if locked_domain.status == Domain.Status.DISABLED:
            return locked_domain
        if locked_domain.status != Domain.Status.PROVISIONING:
            return locked_domain
        build_dns_instructions(
            locked_domain,
            ownership_token=(
                locked_domain.dns_records.filter(
                    purpose=DomainDNSRecord.Purpose.OWNERSHIP,
                    name=application_ownership_record_name(locked_domain),
                )
                .values_list("value", flat=True)
                .first()
                or secrets.token_urlsafe(32)
            ),
            verification_token=verification_token,
            dkim_tokens=dkim_tokens,
        )
        locked_domain.ses_identity_status = verification_status or "PENDING"
        locked_domain.ses_identity_origin = (
            Domain.SESIdentityOrigin.MANAGED
            if may_manage_identity
            else Domain.SESIdentityOrigin.ADOPTION_PENDING
        )
        locked_domain.status = Domain.Status.PENDING_DNS
        locked_domain.error_code = ""
        locked_domain.error_message = ""
        locked_domain.save(
            update_fields=(
                "ses_identity_status",
                "ses_identity_origin",
                "status",
                "error_code",
                "error_message",
                "updated_at",
            )
        )
        if locked_domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTION_PENDING:
            AuditEvent.objects.get_or_create(
                organization=locked_domain.organization,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type="domain.ses_identity_adoption_pending",
                object_type="Domain",
                object_id=locked_domain.id,
                request_id=f"provision:{locked_domain.id}:existing-identity",
                defaults={
                    "metadata": {
                        "ses_verification_status": locked_domain.ses_identity_status,
                        "dkim_instructions_available": bool(dkim_tokens),
                    }
                },
            )
        return locked_domain


def reconcile_ses_identity_adoption(
    domain: Domain,
    *,
    ses_verification_status: str,
    dkim_verification_status: str,
    ses_client=None,
) -> tuple[str, str] | None:
    """Safely restart a claimed pre-existing SES identity when AWS reports failure."""

    if domain.ses_identity_origin not in {
        Domain.SESIdentityOrigin.ADOPTION_PENDING,
        Domain.SESIdentityOrigin.ADOPTED,
    }:
        return None
    if not domain.dns_records.filter(
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        name=application_ownership_record_name(domain),
        status=DomainDNSRecord.Status.VALID,
    ).exists():
        return None

    verification_status = str(ses_verification_status).upper()
    dkim_status = str(dkim_verification_status).upper()
    has_verification_instruction = domain.dns_records.filter(
        purpose=DomainDNSRecord.Purpose.SES_VERIFICATION
    ).exists()
    has_dkim_instructions = domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exists()
    restart_verification = verification_status not in {"PENDING", "SUCCESS"} or (
        verification_status == "PENDING" and not has_verification_instruction
    )
    restart_dkim = dkim_status not in {"PENDING", "SUCCESS"} or (
        dkim_status == "PENDING" and not has_dkim_instructions
    )
    if not restart_verification and not restart_dkim:
        return verification_status, dkim_status

    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    if restart_verification:
        verification = client.verify_domain_identity(Domain=domain.hostname)
        _upsert_dns_instruction(
            domain,
            purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
            record_type="TXT",
            name=f"_amazonses.{domain.hostname}",
            value=str(verification["VerificationToken"]),
            is_required=domain.setup_mode == Domain.SetupMode.DIRECT_MX,
        )
        verification_status = "PENDING"
    if restart_dkim:
        dkim = client.verify_domain_dkim(Domain=domain.hostname)
        dkim_tokens = [str(value) for value in dkim.get("DkimTokens", [])]
        if not dkim_tokens:
            raise RuntimeError("Amazon SES did not return DKIM tokens for the adopted identity.")
        for token in dkim_tokens:
            _upsert_dns_instruction(
                domain,
                purpose=DomainDNSRecord.Purpose.DKIM,
                record_type="CNAME",
                name=f"{token}._domainkey.{domain.hostname}",
                value=f"{token}.dkim.amazonses.com",
                is_required=False,
            )
        if dkim_tokens:
            current_dkim_names = {f"{token}._domainkey.{domain.hostname}" for token in dkim_tokens}
            domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exclude(
                name__in=current_dkim_names
            ).delete()
        dkim_status = "PENDING"

    domain.ses_identity_status = verification_status
    domain.save(update_fields=("ses_identity_status", "updated_at"))
    AuditEvent.objects.create(
        organization=domain.organization,
        actor_type=AuditEvent.ActorType.SYSTEM,
        event_type="domain.ses_identity_reinitialized",
        object_type="Domain",
        object_id=domain.id,
        request_id=f"dns:{domain.id}:reinit:{secrets.token_hex(4)}",
        metadata={
            "ses_verification_restarted": restart_verification,
            "dkim_restarted": restart_dkim,
        },
    )
    getattr(domain, "_prefetched_objects_cache", {}).pop("dns_records", None)
    return verification_status, dkim_status


def _assert_domain_test_ready(domain: Domain) -> None:
    required_records = domain.dns_records.filter(is_required=True)
    if (
        domain.status != Domain.Status.PENDING_TEST
        or not domain.ownership_verified
        or (
            domain.setup_mode == Domain.SetupMode.DIRECT_MX
            and domain.ses_identity_status != "SUCCESS"
        )
        or not required_records.exists()
        or required_records.exclude(status=DomainDNSRecord.Status.VALID).exists()
        or not domain.inbound_routes.filter(is_active=True).exists()
    ):
        raise ValidationError("Verify the required DNS records before generating a test address.")


def _assert_domain_test_cooldown(domain: Domain, *, now=None) -> None:
    now = now or timezone.now()
    cooldown_started_at = now - timedelta(seconds=settings.DOMAIN_TEST_COOLDOWN_SECONDS)
    if domain.tests.filter(created_at__gte=cooldown_started_at).exists():
        raise ValidationError(
            "A test address was generated recently. Use that address or wait a minute "
            "before generating another one."
        )


def create_domain_test(
    domain: Domain, *, receipt_rule_reconciler: Callable[[], object] | None = None
) -> tuple[DomainTest, str]:
    _assert_domain_test_ready(domain)
    _assert_domain_test_cooldown(domain)
    if receipt_rule_reconciler is None:
        from inbox.services.receipt_rules import reconcile_receipt_rule

        receipt_rule_reconciler = reconcile_receipt_rule
    # Ensure SES can actually accept the test before revealing a one-time
    # address. Keep this network call outside the SQLite write transaction.
    receipt_rule_reconciler()

    raw = secrets.token_urlsafe(24).lower()
    local_part = f"test-{raw}"
    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        _assert_domain_test_ready(locked_domain)
        _assert_domain_test_cooldown(locked_domain)
        test = DomainTest.objects.create(
            organization=locked_domain.organization,
            domain=locked_domain,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=24),
        )
    # The customer sends to its own domain. Direct mode therefore exercises the
    # customer MX; forwarding mode exercises the provider catch-all before the
    # envelope reaches the tenant's high-entropy service route.
    return test, f"{local_part}@{locked_domain.hostname}"


def apply_domain_readiness(
    domain: Domain,
    *,
    ses_verification_status: str | None = None,
    dkim_verification_status: str | None = None,
    now=None,
) -> bool:
    """Derive separate ownership, inbound, and outbound readiness from observations."""
    now = now or timezone.now()
    if domain.status in {Domain.Status.ERROR, Domain.Status.DISABLED}:
        return False
    previous_ownership = domain.ownership_verified
    records = list(domain.dns_records.all())
    requires_fresh_application_proof = domain.ses_identity_origin in {
        Domain.SESIdentityOrigin.ADOPTION_PENDING,
        Domain.SESIdentityOrigin.ADOPTED,
    }
    ownership_dns = any(
        item.purpose == DomainDNSRecord.Purpose.OWNERSHIP
        and item.status == DomainDNSRecord.Status.VALID
        and (
            not requires_fresh_application_proof
            or item.name == application_ownership_record_name(domain)
        )
        for item in records
    )
    required_valid = bool(records) and all(
        not item.is_required or item.status == DomainDNSRecord.Status.VALID for item in records
    )
    observed_ses_status = str(
        ses_verification_status
        if ses_verification_status is not None
        else domain.ses_identity_status
    ).upper()
    ses_receiving_ready = (
        domain.setup_mode != Domain.SetupMode.DIRECT_MX or observed_ses_status == "SUCCESS"
    )
    test_received = domain.tests.filter(status=DomainTest.Status.RECEIVED).exists()

    domain.ownership_verified = ownership_dns
    if ownership_dns and domain.verified_at is None:
        domain.verified_at = now
    elif not ownership_dns:
        domain.verified_at = None
    adoption_completed = (
        ownership_dns and domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTION_PENDING
    )
    if adoption_completed:
        domain.ses_identity_origin = Domain.SESIdentityOrigin.ADOPTED

    if ses_verification_status is not None:
        domain.ses_identity_status = ses_verification_status.upper()
    if ses_verification_status is not None or dkim_verification_status is not None:
        domain.outbound_ready = (
            ownership_dns
            and str(ses_verification_status).upper() == "SUCCESS"
            and str(dkim_verification_status).upper() == "SUCCESS"
        )
    elif not ownership_dns:
        # Never preserve send authorization after current DNS ownership proof
        # disappears, even when SES observations are temporarily unavailable.
        domain.outbound_ready = False

    domain.inbound_ready = (
        ownership_dns and required_valid and ses_receiving_ready and test_received
    )
    if not ownership_dns or not required_valid:
        if domain.status in {Domain.Status.READY, Domain.Status.DEGRADED}:
            domain.status = Domain.Status.DEGRADED
            domain.error_code = "dns_drift"
            domain.error_message = "A required ownership or routing DNS record no longer matches."
        else:
            domain.status = Domain.Status.PENDING_DNS
    elif not ses_receiving_ready:
        if domain.status in {Domain.Status.READY, Domain.Status.DEGRADED}:
            domain.status = Domain.Status.DEGRADED
            domain.error_code = "ses_identity_not_ready"
            domain.error_message = (
                "Amazon SES no longer reports this direct-receiving identity as verified."
            )
        else:
            domain.status = Domain.Status.PENDING_DNS
    elif domain.inbound_ready:
        domain.status = Domain.Status.READY
        domain.error_code = ""
        domain.error_message = ""
    else:
        domain.status = Domain.Status.PENDING_TEST
        domain.error_code = ""
        domain.error_message = ""
    domain.last_checked_at = now
    domain.save()
    if adoption_completed:
        AuditEvent.objects.get_or_create(
            organization=domain.organization,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.ses_identity_adopted",
            object_type="Domain",
            object_id=domain.id,
            request_id=f"dns:{domain.id}:identity-adopted",
            defaults={"metadata": {"ownership_record": application_ownership_record_name(domain)}},
        )
    return previous_ownership != domain.ownership_verified
