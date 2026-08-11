from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

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
    DurableJob,
    InboundRoute,
    InboundRoutingTransition,
    RetentionPolicy,
    User,
)


class DomainLimitError(ValidationError):
    pass


class DomainClaimLookupError(ValidationError):
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


class MXLayout(StrEnum):
    NONE = "NONE"
    OPERATIONAL_INBOX = "OPERATIONAL_INBOX"
    EXTERNAL = "EXTERNAL"
    MIXED = "MIXED"


class DomainRoutingClassification(StrEnum):
    NO_MX = "NO_MX"
    OPERATIONAL_INBOX_RECONNECT = "OPERATIONAL_INBOX_RECONNECT"
    SES_MX_UNCLAIMED = "SES_MX_UNCLAIMED"
    EXTERNAL_MX = "EXTERNAL_MX"
    MIXED_MX = "MIXED_MX"


@dataclass(frozen=True)
class DomainRoutingInspection:
    classification: DomainRoutingClassification
    mx_records: tuple[MXObservation, ...]
    # None means the TXT record was irrelevant to the observed MX layout and
    # therefore was intentionally not queried.
    has_operational_inbox_claim: bool | None
    recommended_setup_mode: str | None

    @property
    def requires_explicit_choice(self) -> bool:
        return self.recommended_setup_mode is None


def expected_inbound_mx_exchange() -> str:
    return f"inbound-smtp.{settings.AWS_REGION}.amazonaws.com"


def operational_inbox_claim_record_name(hostname: str) -> str:
    return f"_operational-inbox-claim.{hostname}"


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


def inspect_operational_inbox_claim(
    hostname: str, resolver: dns.resolver.Resolver | None = None
) -> bool:
    resolver = resolver or dns.resolver.Resolver()
    record_name = operational_inbox_claim_record_name(hostname)
    try:
        answers = resolver.resolve(record_name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except dns.resolver.NoNameservers as exc:
        raise DomainClaimLookupError(
            "The domain's nameservers did not answer the ownership-record lookup. "
            "Try again shortly."
        ) from exc
    except (dns.exception.Timeout, dns.resolver.LifetimeTimeout) as exc:
        raise DomainClaimLookupError(
            "The ownership-record lookup timed out. Try again shortly."
        ) from exc
    return any(True for _ in answers)


def classify_mx_layout(mx_records: list[MXObservation] | tuple[MXObservation, ...]) -> MXLayout:
    if not mx_records:
        return MXLayout.NONE
    expected_exchange = expected_inbound_mx_exchange().casefold()
    expected_matches = [
        item.exchange.rstrip(".").casefold() == expected_exchange for item in mx_records
    ]
    if all(expected_matches):
        return MXLayout.OPERATIONAL_INBOX
    if any(expected_matches):
        return MXLayout.MIXED
    return MXLayout.EXTERNAL


def classify_domain_routing(
    mx_records: list[MXObservation] | tuple[MXObservation, ...],
    *,
    has_operational_inbox_claim: bool | None,
) -> DomainRoutingInspection:
    records = tuple(mx_records)
    layout = classify_mx_layout(records)
    if layout == MXLayout.NONE:
        classification = DomainRoutingClassification.NO_MX
        recommended_setup_mode = Domain.SetupMode.DIRECT_MX
    elif layout == MXLayout.OPERATIONAL_INBOX and has_operational_inbox_claim is True:
        classification = DomainRoutingClassification.OPERATIONAL_INBOX_RECONNECT
        recommended_setup_mode = Domain.SetupMode.DIRECT_MX
    elif layout == MXLayout.OPERATIONAL_INBOX:
        # SES inbound endpoints are shared across AWS customers. Without an
        # Operational Inbox-specific historical hint, require an explicit choice.
        classification = DomainRoutingClassification.SES_MX_UNCLAIMED
        recommended_setup_mode = None
    elif layout == MXLayout.MIXED:
        classification = DomainRoutingClassification.MIXED_MX
        recommended_setup_mode = None
    else:
        classification = DomainRoutingClassification.EXTERNAL_MX
        recommended_setup_mode = Domain.SetupMode.PROVIDER_FORWARD
    return DomainRoutingInspection(
        classification=classification,
        mx_records=records,
        has_operational_inbox_claim=has_operational_inbox_claim,
        recommended_setup_mode=recommended_setup_mode,
    )


def inspect_domain_routing(
    hostname: str, resolver: dns.resolver.Resolver | None = None
) -> DomainRoutingInspection:
    normalized = normalize_hostname(hostname)
    resolver = resolver or dns.resolver.Resolver()
    mx_records = inspect_mx(normalized, resolver=resolver)
    # The historical claim only changes the interpretation of an all-SES MX
    # layout. Do not let an unrelated TXT lookup block no-MX, external-provider,
    # or already-ambiguous mixed-MX setup choices.
    has_claim: bool | None = None
    if classify_mx_layout(mx_records) == MXLayout.OPERATIONAL_INBOX:
        has_claim = inspect_operational_inbox_claim(normalized, resolver=resolver)
    return classify_domain_routing(
        mx_records,
        has_operational_inbox_claim=has_claim,
    )


def classify_stored_mx(records: list[dict[str, object]]) -> MXLayout:
    observations = []
    for record in records:
        try:
            raw_preference = record["preference"]
            raw_exchange = record["exchange"]
            if (
                isinstance(raw_preference, bool)
                or not isinstance(raw_preference, (int, str))
                or not isinstance(raw_exchange, str)
            ):
                return MXLayout.MIXED
            observations.append(
                MXObservation(
                    preference=int(raw_preference),
                    exchange=raw_exchange,
                )
            )
        except (KeyError, TypeError, ValueError):
            return MXLayout.MIXED
    return classify_mx_layout(observations)


def _assert_limits(owner: User) -> None:
    from inbox.services.entitlements import for_user

    entitlements = for_user(owner)
    domain_limit = entitlements.domain_limit
    active_domain_count = (
        Domain.objects.filter(owner=owner).exclude(status=Domain.Status.DISABLED).count()
    )
    if active_domain_count >= domain_limit:
        raise DomainLimitError(
            f"Active domain capacity is {domain_limit}; current usage is {active_domain_count}. "
            "Disable a domain before connecting another.",
            code="capacity_reached",
            params={
                "resource": "active_domains",
                "used": active_domain_count,
                "limit": domain_limit,
                "remaining": 0,
                "reset_at": None,
                "retryable": False,
            },
        )
    since = timezone.now() - timedelta(seconds=settings.DOMAIN_PROVISION_RATE_WINDOW_SECONDS)
    recent = Domain.objects.filter(owner=owner, created_at__gte=since).count()
    if recent >= settings.DOMAIN_PROVISION_RATE_LIMIT:
        raise DomainLimitError(
            "Domain provisioning is temporarily rate limited. Try again later.",
            code="rate_limited",
        )


def create_domain(*, owner: User, hostname: str, setup_mode: str) -> Domain:
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
            existing_domain=existing if existing.owner_id == owner.id else None
        )
    try:
        with transaction.atomic():
            _assert_limits(owner)
            domain = Domain.objects.create(
                owner=owner,
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
            existing_domain=existing if existing.owner_id == owner.id else None
        ) from exc
    RetentionPolicy.objects.create(domain=domain)
    return domain


def recommended_setup(domain: Domain) -> str:
    layout = classify_stored_mx(domain.existing_mx)
    if layout == MXLayout.EXTERNAL:
        return Domain.SetupMode.PROVIDER_FORWARD
    return domain.setup_mode


def application_ownership_record_name(domain: Domain) -> str:
    return operational_inbox_claim_record_name(domain.hostname)


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


def build_inbound_dns_instructions(
    domain: Domain,
    *,
    ownership_token: str,
    verification_token: str = "",
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
            value=expected_inbound_mx_exchange(),
            priority=10,
            is_required=True,
        )


def build_outbound_dns_instructions(
    domain: Domain,
    *,
    verification_token: str,
    dkim_tokens: list[str],
) -> None:
    if verification_token:
        _upsert_dns_instruction(
            domain,
            purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
            record_type="TXT",
            name=f"_amazonses.{domain.hostname}",
            value=verification_token,
            is_required=domain.setup_mode == Domain.SetupMode.DIRECT_MX,
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


def build_dns_instructions(
    domain: Domain,
    *,
    ownership_token: str,
    verification_token: str,
    dkim_tokens: list[str],
) -> None:
    """Build the complete legacy checklist; capability provisioning uses split helpers."""

    build_inbound_dns_instructions(
        domain,
        ownership_token=ownership_token,
        verification_token=verification_token,
    )
    build_outbound_dns_instructions(
        domain,
        verification_token=verification_token,
        dkim_tokens=dkim_tokens,
    )


def expire_unverified_claims() -> int:
    now = timezone.now()
    with transaction.atomic():
        # Lock and re-evaluate candidates in one transaction. A routing-mode
        # switch renews the claim under the same row lock, so a claim selected
        # by an earlier snapshot cannot disable the newer setup generation.
        expired = list(
            Domain.objects.select_for_update().filter(
                ownership_verified=False,
                claim_expires_at__lte=now,
                status__in=[Domain.Status.PROVISIONING, Domain.Status.PENDING_DNS],
            )
        )
        for domain in expired:
            domain.status = Domain.Status.DISABLED
            domain.inbound_ready = False
            domain.outbound_ready = False
            domain.outbound_status = Domain.OutboundStatus.DISABLED
            domain.error_code = "claim_expired"
            domain.error_message = "The ownership claim expired before verification."
            domain.save(
                update_fields=(
                    "status",
                    "inbound_ready",
                    "outbound_ready",
                    "outbound_status",
                    "error_code",
                    "error_message",
                    "updated_at",
                )
            )
            domain.inbound_routes.update(is_active=False)
            domain.routing_transitions.filter(
                status__in=(
                    InboundRoutingTransition.Status.PREPARING,
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                    InboundRoutingTransition.Status.GRACE,
                    InboundRoutingTransition.Status.FAILED,
                )
            ).update(
                status=InboundRoutingTransition.Status.CANCELLED,
                cancelled_at=now,
                updated_at=now,
            )
            domain.tests.filter(
                status=DomainTest.Status.PENDING,
                routing_transition__isnull=False,
            ).update(status=DomainTest.Status.EXPIRED, updated_at=now)
            DurableJob.objects.get_or_create(
                idempotency_key=f"receipt-rule:claim-expired:{domain.id}",
                defaults={
                    "domain": domain,
                    "kind": "reconcile_receipt_rule",
                    "payload": {},
                    "due_at": now,
                },
            )
    return len(expired)


def _matches_inbound_provisioning_attempt(
    domain: Domain,
    *,
    expected_generation: int,
    expected_setup_mode: str,
) -> bool:
    return (
        domain.status == Domain.Status.PROVISIONING
        and domain.inbound_setup_generation == expected_generation
        and domain.setup_mode == expected_setup_mode
        and not domain.routing_transitions.filter(
            status__in=(
                InboundRoutingTransition.Status.PREPARING,
                InboundRoutingTransition.Status.WAITING_DNS,
                InboundRoutingTransition.Status.WAITING_TEST,
                InboundRoutingTransition.Status.GRACE,
                InboundRoutingTransition.Status.FAILED,
            )
        ).exists()
    )


def provision_inbound(
    domain: Domain,
    *,
    expected_generation: int | None = None,
    expected_setup_mode: str | None = None,
    ses_client=None,
) -> Domain:
    """Prepare receiving instructions without provisioning optional sending."""

    expected_generation = expected_generation or domain.inbound_setup_generation
    expected_setup_mode = expected_setup_mode or domain.setup_mode
    if not _matches_inbound_provisioning_attempt(
        domain,
        expected_generation=expected_generation,
        expected_setup_mode=expected_setup_mode,
    ):
        return domain

    if expected_setup_mode == Domain.SetupMode.PROVIDER_FORWARD:
        with transaction.atomic():
            locked_domain = Domain.objects.select_for_update().get(id=domain.id)
            if not _matches_inbound_provisioning_attempt(
                locked_domain,
                expected_generation=expected_generation,
                expected_setup_mode=expected_setup_mode,
            ):
                return locked_domain
            build_inbound_dns_instructions(
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
            )
            locked_domain.status = Domain.Status.PENDING_DNS
            locked_domain.error_code = ""
            locked_domain.error_message = ""
            locked_domain.save(
                update_fields=("status", "error_code", "error_message", "updated_at")
            )
            return locked_domain

    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    attributes = client.get_identity_verification_attributes(Identities=[domain.hostname]).get(
        "VerificationAttributes", {}
    )
    identity_attributes = attributes.get(domain.hostname, {})
    identity_exists = domain.hostname in attributes
    may_manage_identity = domain.ses_identity_origin in {
        Domain.SESIdentityOrigin.MANAGED,
        Domain.SESIdentityOrigin.ADOPTED,
    }
    verification_token = str(identity_attributes.get("VerificationToken", ""))
    verification_status = str(identity_attributes.get("VerificationStatus", "")).upper()

    if not identity_exists:
        # Persist intent before the AWS calls. A retry can then distinguish an
        # identity this application started creating from an unrelated existing
        # identity without overloading the observed SES verification status.
        intent_recorded = Domain.objects.filter(
            id=domain.id,
            status=Domain.Status.PROVISIONING,
            inbound_setup_generation=expected_generation,
            setup_mode=expected_setup_mode,
        ).update(
            ses_identity_status="PROVISIONING",
            ses_identity_origin=Domain.SESIdentityOrigin.MANAGED,
            updated_at=timezone.now(),
        )
        if not intent_recorded:
            domain.refresh_from_db()
            return domain
        domain.ses_identity_status = "PROVISIONING"
        domain.ses_identity_origin = Domain.SESIdentityOrigin.MANAGED
        may_manage_identity = True

    if may_manage_identity and (
        verification_status not in {"PENDING", "SUCCESS"}
        or (verification_status == "PENDING" and not verification_token)
    ):
        # Re-check immediately before the account-scoped write. Final state is
        # fenced again under a row lock below, but stale attempts should avoid
        # starting even an idempotent SES verification whenever possible.
        if not Domain.objects.filter(
            id=domain.id,
            status=Domain.Status.PROVISIONING,
            inbound_setup_generation=expected_generation,
            setup_mode=expected_setup_mode,
        ).exists():
            domain.refresh_from_db()
            return domain
        verification = client.verify_domain_identity(Domain=domain.hostname)
        verification_token = str(verification["VerificationToken"])
        verification_status = "PENDING"

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        if not _matches_inbound_provisioning_attempt(
            locked_domain,
            expected_generation=expected_generation,
            expected_setup_mode=expected_setup_mode,
        ):
            return locked_domain
        build_inbound_dns_instructions(
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
                domain=locked_domain,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type="domain.ses_identity_adoption_pending",
                object_type="Domain",
                object_id=locked_domain.id,
                request_id=f"provision:{locked_domain.id}:existing-identity",
                defaults={
                    "metadata": {
                        "ses_verification_status": locked_domain.ses_identity_status,
                    }
                },
            )
        return locked_domain


def provision_outbound_identity(domain: Domain, *, ses_client=None) -> Domain:
    """Prepare SES identity and DKIM records without changing inbound state."""

    if domain.status == Domain.Status.DISABLED:
        return domain
    if domain.outbound_status != Domain.OutboundStatus.PROVISIONING:
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
    has_fresh_ownership = (
        domain.ownership_verified
        and domain.dns_records.filter(
            purpose=DomainDNSRecord.Purpose.OWNERSHIP,
            name=application_ownership_record_name(domain),
            status=DomainDNSRecord.Status.VALID,
        ).exists()
    )
    may_manage_identity = domain.ses_identity_origin in {
        Domain.SESIdentityOrigin.MANAGED,
        Domain.SESIdentityOrigin.ADOPTED,
    }
    verification_token = str(identity_attributes.get("VerificationToken", ""))
    verification_status = str(identity_attributes.get("VerificationStatus", "")).upper()
    dkim_status = str(identity_dkim_attributes.get("DkimVerificationStatus", "")).upper()
    dkim_tokens = [str(value) for value in identity_dkim_attributes.get("DkimTokens", [])]

    if not identity_exists:
        domain.ses_identity_status = "PROVISIONING"
        domain.ses_identity_origin = Domain.SESIdentityOrigin.MANAGED
        domain.save(update_fields=("ses_identity_status", "ses_identity_origin", "updated_at"))
        may_manage_identity = True
    elif not domain.ses_identity_origin:
        domain.ses_identity_origin = (
            Domain.SESIdentityOrigin.ADOPTED
            if has_fresh_ownership
            else Domain.SESIdentityOrigin.ADOPTION_PENDING
        )
        domain.save(update_fields=("ses_identity_origin", "updated_at"))
        may_manage_identity = has_fresh_ownership

    if may_manage_identity and (
        verification_status not in {"PENDING", "SUCCESS"}
        or (verification_status == "PENDING" and not verification_token)
    ):
        verification = client.verify_domain_identity(Domain=domain.hostname)
        verification_token = str(verification["VerificationToken"])
        verification_status = "PENDING"
    if may_manage_identity and (not dkim_tokens or dkim_status == "FAILED"):
        dkim = client.verify_domain_dkim(Domain=domain.hostname)
        dkim_tokens = [str(value) for value in dkim.get("DkimTokens", [])]
        if not dkim_tokens:
            raise RuntimeError("Amazon SES did not return DKIM tokens for the sending identity.")
        dkim_status = "PENDING"

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        if locked_domain.status == Domain.Status.DISABLED:
            return locked_domain
        if locked_domain.outbound_status != Domain.OutboundStatus.PROVISIONING:
            return locked_domain
        build_outbound_dns_instructions(
            locked_domain,
            verification_token=verification_token,
            dkim_tokens=dkim_tokens,
        )
        locked_domain.ses_identity_status = verification_status or "PENDING"
        locked_domain.outbound_status = Domain.OutboundStatus.PENDING_DNS
        locked_domain.outbound_ready = False
        locked_domain.outbound_error_code = ""
        locked_domain.outbound_error_message = ""
        locked_domain.save(
            update_fields=(
                "ses_identity_status",
                "outbound_status",
                "outbound_ready",
                "outbound_error_code",
                "outbound_error_message",
                "updated_at",
            )
        )
        if locked_domain.ses_identity_origin == Domain.SESIdentityOrigin.ADOPTION_PENDING:
            AuditEvent.objects.get_or_create(
                domain=locked_domain,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type="domain.ses_identity_adoption_pending",
                object_type="Domain",
                object_id=locked_domain.id,
                request_id=f"outbound:{locked_domain.id}:existing-identity",
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
    outbound_enabled = domain.outbound_status != Domain.OutboundStatus.DISABLED
    has_direct_transition = domain.routing_transitions.filter(
        to_mode=Domain.SetupMode.DIRECT_MX,
        status__in=(
            InboundRoutingTransition.Status.PREPARING,
            InboundRoutingTransition.Status.WAITING_DNS,
            InboundRoutingTransition.Status.WAITING_TEST,
        ),
    ).exists()
    needs_ses_verification = (
        domain.setup_mode == Domain.SetupMode.DIRECT_MX or outbound_enabled or has_direct_transition
    )
    has_verification_instruction = domain.dns_records.filter(
        purpose=DomainDNSRecord.Purpose.SES_VERIFICATION
    ).exists()
    has_dkim_instructions = domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.DKIM).exists()
    restart_verification = needs_ses_verification and (
        verification_status not in {"PENDING", "SUCCESS"}
        or (verification_status == "PENDING" and not has_verification_instruction)
    )
    restart_dkim = outbound_enabled and (
        dkim_status not in {"PENDING", "SUCCESS"}
        or (dkim_status == "PENDING" and not has_dkim_instructions)
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
        domain=domain,
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
    expected_route_kind = (
        InboundRoute.Kind.DIRECT_DOMAIN
        if domain.setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )
    if (
        domain.status != Domain.Status.PENDING_TEST
        or not domain.ownership_verified
        or (
            domain.setup_mode == Domain.SetupMode.DIRECT_MX
            and domain.ses_identity_status != "SUCCESS"
        )
        or not required_records.exists()
        or required_records.exclude(status=DomainDNSRecord.Status.VALID).exists()
        or not domain.inbound_routes.filter(
            is_active=True,
            kind=expected_route_kind,
            setup_generation=domain.inbound_setup_generation,
        ).exists()
        or domain.routing_transitions.filter(
            status__in=(
                InboundRoutingTransition.Status.PREPARING,
                InboundRoutingTransition.Status.WAITING_DNS,
                InboundRoutingTransition.Status.WAITING_TEST,
                InboundRoutingTransition.Status.GRACE,
                InboundRoutingTransition.Status.FAILED,
            )
        ).exists()
    ):
        raise ValidationError("Verify the required DNS records before preparing the test address.")


def _test_address_matches(test: DomainTest, hostname: str) -> bool:
    if not test.address or "@" not in test.address:
        return False
    local_part, address_domain = test.address.rsplit("@", 1)
    if address_domain.casefold() != hostname.casefold() or not local_part.startswith("test-"):
        return False
    raw = local_part.removeprefix("test-")
    return bool(raw) and secrets.compare_digest(
        hashlib.sha256(raw.encode()).hexdigest(),
        test.token_hash,
    )


def _current_locked_domain_test(domain: Domain, *, now) -> DomainTest | None:
    expected_route_kind = (
        InboundRoute.Kind.DIRECT_DOMAIN
        if domain.setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )
    current = None
    expire_ids = []
    pending_tests = domain.tests.select_for_update().filter(status=DomainTest.Status.PENDING)
    for test in pending_tests.order_by("-created_at", "-id"):
        exact_current_scope = (
            test.routing_transition_id is None
            and test.setup_generation == domain.inbound_setup_generation
            and test.expected_setup_mode == domain.setup_mode
            and test.expected_route_kind == expected_route_kind
            and test.expires_at > now
            and test.received_message_id is None
            and _test_address_matches(test, domain.hostname)
        )
        if exact_current_scope and current is None:
            current = test
        else:
            expire_ids.append(test.id)
    if expire_ids:
        DomainTest.objects.filter(id__in=expire_ids).update(
            status=DomainTest.Status.EXPIRED,
            updated_at=now,
        )
    return current


def ensure_domain_test(
    domain: Domain, *, receipt_rule_reconciler: Callable[[], object] | None = None
) -> tuple[DomainTest, str, bool]:
    """Return the one current setup test, creating it only when none remains usable."""

    now = timezone.now()
    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        _assert_domain_test_ready(locked_domain)
        current = _current_locked_domain_test(locked_domain, now=now)
        if current is not None:
            return current, str(current.address), False
        direct_mx = locked_domain.setup_mode == Domain.SetupMode.DIRECT_MX

    if direct_mx:
        if receipt_rule_reconciler is None:
            from inbox.services.receipt_rules import reconcile_receipt_rule

            receipt_rule_reconciler = reconcile_receipt_rule
        # Do not reveal a direct-MX test target before SES accepts this domain.
        # The AWS call stays outside the row-locking transaction.
        receipt_rule_reconciler()

    now = timezone.now()
    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        _assert_domain_test_ready(locked_domain)
        current = _current_locked_domain_test(locked_domain, now=now)
        if current is not None:
            return current, str(current.address), False

        raw = secrets.token_urlsafe(24).lower()
        address = f"test-{raw}@{locked_domain.hostname}"
        test = DomainTest.objects.create(
            domain=locked_domain,
            setup_generation=locked_domain.inbound_setup_generation,
            expected_setup_mode=locked_domain.setup_mode,
            expected_route_kind=(
                InboundRoute.Kind.DIRECT_DOMAIN
                if locked_domain.setup_mode == Domain.SetupMode.DIRECT_MX
                else InboundRoute.Kind.FORWARDING_ALIAS
            ),
            address=address,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=now + timedelta(hours=24),
        )
        return test, address, True


def create_domain_test(
    domain: Domain, *, receipt_rule_reconciler: Callable[[], object] | None = None
) -> tuple[DomainTest, str]:
    test, address, _ = ensure_domain_test(
        domain,
        receipt_rule_reconciler=receipt_rule_reconciler,
    )
    return test, address


def apply_domain_readiness(
    domain: Domain,
    *,
    ses_verification_status: str | None = None,
    dkim_verification_status: str | None = None,
    now=None,
) -> bool:
    """Derive separate ownership, inbound, and outbound readiness from observations."""
    now = now or timezone.now()
    if domain.status in {
        Domain.Status.PROVISIONING,
        Domain.Status.ERROR,
        Domain.Status.DISABLED,
    }:
        return False
    observed_generation = domain.inbound_setup_generation
    observed_status = domain.status
    observed_outbound_status = domain.outbound_status
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
    transitioning_to_provider = domain.routing_transitions.filter(
        from_mode=Domain.SetupMode.DIRECT_MX,
        to_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=InboundRoutingTransition.Status.WAITING_TEST,
    ).exists()
    required_valid = bool(records) and all(
        not item.is_required
        or item.status == DomainDNSRecord.Status.VALID
        or (transitioning_to_provider and item.purpose == DomainDNSRecord.Purpose.MX)
        for item in records
    )
    observed_ses_status = str(
        ses_verification_status
        if ses_verification_status is not None
        else domain.ses_identity_status
    ).upper()
    observed_dkim_status = str(dkim_verification_status or "").upper()
    ses_receiving_ready = (
        domain.setup_mode != Domain.SetupMode.DIRECT_MX or observed_ses_status == "SUCCESS"
    )
    expected_route_kind = (
        InboundRoute.Kind.DIRECT_DOMAIN
        if domain.setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )
    test_received = domain.tests.filter(
        status=DomainTest.Status.RECEIVED,
        setup_generation=domain.inbound_setup_generation,
        expected_setup_mode=domain.setup_mode,
        expected_route_kind=expected_route_kind,
    ).exists()
    active_route_ready = domain.inbound_routes.filter(
        is_active=True,
        setup_generation=domain.inbound_setup_generation,
        kind=expected_route_kind,
    ).exists()

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

    dkim_records = [item for item in records if item.purpose == DomainDNSRecord.Purpose.DKIM]
    dkim_dns_ready = bool(dkim_records) and all(
        item.status == DomainDNSRecord.Status.VALID for item in dkim_records
    )
    outbound_observed = ses_verification_status is not None and dkim_verification_status is not None
    if domain.outbound_status == Domain.OutboundStatus.DISABLED:
        domain.outbound_ready = False
        domain.outbound_error_code = ""
        domain.outbound_error_message = ""
    elif domain.outbound_status not in {
        Domain.OutboundStatus.PROVISIONING,
        Domain.OutboundStatus.ERROR,
    }:
        outbound_ready = (
            ownership_dns
            and dkim_dns_ready
            and outbound_observed
            and observed_ses_status == "SUCCESS"
            and observed_dkim_status == "SUCCESS"
        )
        if outbound_ready:
            domain.outbound_ready = True
            domain.outbound_status = Domain.OutboundStatus.READY
            domain.outbound_error_code = ""
            domain.outbound_error_message = ""
        elif outbound_observed or not ownership_dns or not dkim_dns_ready:
            was_ready = domain.outbound_status in {
                Domain.OutboundStatus.READY,
                Domain.OutboundStatus.DEGRADED,
            }
            domain.outbound_ready = False
            domain.outbound_status = (
                Domain.OutboundStatus.DEGRADED if was_ready else Domain.OutboundStatus.PENDING_DNS
            )
            if was_ready:
                if not ownership_dns or not dkim_dns_ready:
                    domain.outbound_error_code = "outbound_dns_drift"
                    domain.outbound_error_message = (
                        "A required ownership or DKIM DNS record no longer matches."
                    )
                else:
                    domain.outbound_error_code = "outbound_identity_not_ready"
                    domain.outbound_error_message = (
                        "Amazon SES no longer reports this sending identity and DKIM as verified."
                    )
            else:
                domain.outbound_error_code = ""
                domain.outbound_error_message = ""
    elif not ownership_dns:
        # Never preserve send authorization after current DNS ownership proof
        # disappears, even when provisioning observations are unavailable.
        domain.outbound_ready = False

    domain.inbound_ready = (
        ownership_dns
        and required_valid
        and ses_receiving_ready
        and active_route_ready
        and test_received
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
    updated = Domain.objects.filter(
        id=domain.id,
        inbound_setup_generation=observed_generation,
        status=observed_status,
        outbound_status=observed_outbound_status,
    ).update(
        ownership_verified=domain.ownership_verified,
        inbound_ready=domain.inbound_ready,
        outbound_ready=domain.outbound_ready,
        outbound_status=domain.outbound_status,
        ses_identity_status=domain.ses_identity_status,
        ses_identity_origin=domain.ses_identity_origin,
        verified_at=domain.verified_at,
        last_checked_at=domain.last_checked_at,
        status=domain.status,
        error_code=domain.error_code,
        error_message=domain.error_message,
        outbound_error_code=domain.outbound_error_code,
        outbound_error_message=domain.outbound_error_message,
        updated_at=now,
    )
    if not updated:
        # A routing-mode transition or another lifecycle update won the race.
        # Preserve that newer state instead of writing this stale observation.
        domain.refresh_from_db()
        return False
    if observed_status == Domain.Status.PENDING_TEST and domain.status == Domain.Status.PENDING_DNS:
        DomainTest.objects.filter(
            domain=domain,
            routing_transition__isnull=True,
            status=DomainTest.Status.PENDING,
        ).update(status=DomainTest.Status.EXPIRED, updated_at=now)
    if adoption_completed:
        AuditEvent.objects.get_or_create(
            domain=domain,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.ses_identity_adopted",
            object_type="Domain",
            object_id=domain.id,
            request_id=f"dns:{domain.id}:identity-adopted",
            defaults={"metadata": {"ownership_record": application_ownership_record_name(domain)}},
        )
    return previous_ownership != domain.ownership_verified
