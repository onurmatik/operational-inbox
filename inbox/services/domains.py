from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import boto3
import dns.exception
import dns.resolver
import idna
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inbox.models import Domain, DomainDNSRecord, DomainTest, InboundRoute, Organization, Project


class DomainLimitError(ValidationError):
    pass


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
        observations.append(
            MXObservation(preference=int(answer.preference), exchange=exchange)
        )
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
    with transaction.atomic():
        _assert_limits(organization)
        if (
            Domain.objects.filter(hostname=normalized)
            .exclude(status=Domain.Status.DISABLED)
            .exists()
        ):
            raise ValidationError(
                {"hostname": "This domain already has an active or pending ownership claim."}
            )
        domain = Domain.objects.create(
            organization=organization,
            project=project,
            hostname=normalized,
            setup_mode=setup_mode,
            status=Domain.Status.PROVISIONING,
            existing_mx=[
                {"preference": item.preference, "exchange": item.exchange} for item in mx
            ],
            claim_expires_at=timezone.now() + timedelta(
                hours=settings.DOMAIN_CLAIM_TTL_HOURS
            ),
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
    return domain


def recommended_setup(domain: Domain) -> str:
    if domain.existing_mx:
        return Domain.SetupMode.PROVIDER_FORWARD
    return domain.setup_mode


def build_dns_instructions(
    domain: Domain, *, verification_token: str, dkim_tokens: list[str]
) -> None:
    records = [
        {
            "purpose": DomainDNSRecord.Purpose.OWNERSHIP,
            "record_type": "TXT",
            "name": f"_amazonses.{domain.hostname}",
            "value": verification_token,
            "is_required": True,
        }
    ]
    if domain.setup_mode == Domain.SetupMode.DIRECT_MX:
        records.append(
            {
                "purpose": DomainDNSRecord.Purpose.MX,
                "record_type": "MX",
                "name": domain.hostname,
                "value": "inbound-smtp.us-east-1.amazonaws.com",
                "priority": 10,
                "is_required": True,
            }
        )
    records.extend(
        {
            "purpose": DomainDNSRecord.Purpose.DKIM,
            "record_type": "CNAME",
            "name": f"{token}._domainkey.{domain.hostname}",
            "value": f"{token}.dkim.amazonses.com",
            "is_required": False,
        }
        for token in dkim_tokens
    )
    for record in records:
        DomainDNSRecord.objects.update_or_create(
            organization=domain.organization,
            domain=domain,
            purpose=record["purpose"],
            record_type=record["record_type"],
            name=record["name"],
            value=record["value"],
            defaults={
                "priority": record.get("priority"),
                "is_required": record["is_required"],
            },
        )


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
    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    attributes = client.get_identity_verification_attributes(Identities=[domain.hostname]).get(
        "VerificationAttributes", {}
    )
    previously_managed = (
        Domain.objects.filter(hostname=domain.hostname, ses_identity_status="MANAGED")
        .exclude(id=domain.id)
        .exists()
    )
    if (
        domain.hostname in attributes
        and domain.ses_identity_status
        not in {
            "MANAGED",
            "PROVISIONING",
        }
        and not previously_managed
    ):
        domain.status = Domain.Status.ERROR
        domain.error_code = "ses_identity_collision"
        domain.error_message = (
            "This SES identity already exists and was not created by Operational Inbox. "
            "Manual review is required."
        )
        domain.save(update_fields=("status", "error_code", "error_message", "updated_at"))
        return domain
    if domain.hostname not in attributes and domain.ses_identity_status != "PROVISIONING":
        domain.ses_identity_status = "PROVISIONING"
        domain.save(update_fields=("ses_identity_status", "updated_at"))
    verification = client.verify_domain_identity(Domain=domain.hostname)
    dkim = client.verify_domain_dkim(Domain=domain.hostname)
    build_dns_instructions(
        domain,
        verification_token=str(verification["VerificationToken"]),
        dkim_tokens=[str(value) for value in dkim.get("DkimTokens", [])],
    )
    domain.ses_identity_status = "MANAGED"
    domain.status = Domain.Status.PENDING_DNS
    domain.error_code = ""
    domain.error_message = ""
    domain.save(
        update_fields=(
            "ses_identity_status",
            "status",
            "error_code",
            "error_message",
            "updated_at",
        )
    )
    return domain


@transaction.atomic
def create_domain_test(domain: Domain) -> tuple[DomainTest, str]:
    raw = secrets.token_urlsafe(24).lower()
    local_part = f"test-{raw}"
    test = DomainTest.objects.create(
        organization=domain.organization,
        domain=domain,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(hours=24),
    )
    # The customer sends to its own domain. Direct mode therefore exercises the
    # customer MX; forwarding mode exercises the provider catch-all before the
    # envelope reaches the tenant's high-entropy service route.
    return test, f"{local_part}@{domain.hostname}"


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
    ownership_dns = any(
        item.purpose == DomainDNSRecord.Purpose.OWNERSHIP
        and item.status == DomainDNSRecord.Status.VALID
        for item in records
    )
    required_valid = bool(records) and all(
        not item.is_required or item.status == DomainDNSRecord.Status.VALID for item in records
    )
    test_received = domain.tests.filter(status=DomainTest.Status.RECEIVED).exists()

    domain.ownership_verified = ownership_dns
    if ownership_dns and domain.verified_at is None:
        domain.verified_at = now
    elif not ownership_dns:
        domain.verified_at = None

    if ses_verification_status is not None:
        domain.ses_identity_status = ses_verification_status.upper()
    if ses_verification_status is not None or dkim_verification_status is not None:
        domain.outbound_ready = (
            str(ses_verification_status).upper() == "SUCCESS"
            and str(dkim_verification_status).upper() == "SUCCESS"
        )

    domain.inbound_ready = ownership_dns and required_valid and test_received
    if not ownership_dns or not required_valid:
        if domain.status in {Domain.Status.READY, Domain.Status.DEGRADED}:
            domain.status = Domain.Status.DEGRADED
            domain.error_code = "dns_drift"
            domain.error_message = "A required ownership or routing DNS record no longer matches."
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
    return previous_ownership != domain.ownership_verified
