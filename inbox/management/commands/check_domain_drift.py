import json

import boto3
import dns.exception
import dns.resolver
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from inbox.models import AuditEvent, Domain, DomainDNSRecord, InboundRoutingTransition
from inbox.services.domains import (
    apply_domain_readiness,
    ensure_domain_test,
    reconcile_ses_identity_adoption,
)
from inbox.services.notifications import create_domain_drift_notifications
from inbox.services.receipt_rules import reconcile_receipt_rule
from inbox.services.routing_transitions import (
    ACTIVE_TRANSITION_STATUSES,
    ensure_routing_transition_test,
    refresh_routing_transition,
)


def observed_values(record: DomainDNSRecord) -> list[str]:
    try:
        answers = dns.resolver.resolve(record.name, record.record_type, lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except (dns.exception.Timeout, dns.resolver.LifetimeTimeout):
        raise
    values = []
    for answer in answers:
        value = str(answer).strip().strip('"')
        if record.record_type == "MX":
            parts = value.split(maxsplit=1)
            value = parts[-1].rstrip(".")
        elif record.record_type != "TXT":
            value = value.rstrip(".")
        values.append(value)
    return sorted(values)


def values_match(record: DomainDNSRecord, observed: list[str]) -> bool:
    if record.record_type == "TXT":
        # Ownership nonces and SES verification tokens are case-sensitive.
        return record.value in observed
    expected = record.value.rstrip(".").casefold()
    normalized = {item.rstrip(".").casefold() for item in observed}
    return expected in normalized


def _noop_receipt_rule_reconciler() -> object:
    return None


class Command(BaseCommand):
    help = "Check customer-managed DNS records and mark readiness drift without writing DNS."

    def handle(self, *args, **options):
        now = timezone.now()
        checked = invalid = 0
        touched_domains = set()
        active_transition_statuses = (
            InboundRoutingTransition.Status.PREPARING,
            InboundRoutingTransition.Status.WAITING_DNS,
            InboundRoutingTransition.Status.WAITING_TEST,
            InboundRoutingTransition.Status.FAILED,
        )
        records = (
            DomainDNSRecord.objects.filter(
                Q(
                    domain__status__in=[
                        Domain.Status.PENDING_DNS,
                        Domain.Status.PENDING_TEST,
                        Domain.Status.READY,
                        Domain.Status.DEGRADED,
                    ]
                )
                | Q(domain__routing_transitions__status__in=active_transition_statuses)
            )
            .exclude(domain__status=Domain.Status.DISABLED)
            .select_related("domain")
            .distinct()
        )
        for record in records:
            checked += 1
            touched_domains.add(record.domain_id)
            try:
                observed = observed_values(record)
            except (dns.exception.Timeout, dns.resolver.LifetimeTimeout):
                record.error_message = "DNS lookup timed out; the previous state was preserved."
                record.last_checked_at = now
                record.save(update_fields=("error_message", "last_checked_at", "updated_at"))
                continue
            record.observed_values = observed
            record.last_checked_at = now
            if values_match(record, observed):
                record.status = DomainDNSRecord.Status.VALID
                record.error_message = ""
            else:
                record.status = (
                    DomainDNSRecord.Status.MISSING
                    if not observed
                    else DomainDNSRecord.Status.INVALID
                )
                record.error_message = "The observed DNS value does not match the required value."
                if record.is_required:
                    invalid += 1
            record.save(
                update_fields=(
                    "observed_values",
                    "last_checked_at",
                    "status",
                    "error_message",
                    "updated_at",
                )
            )
        domains = list(
            Domain.objects.filter(id__in=touched_domains)
            .prefetch_related("dns_records", "tests", "routing_transitions__routes")
            .order_by("hostname")
        )
        transitions = list(
            InboundRoutingTransition.objects.filter(
                domain__in=domains,
                status__in=(
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                ),
            ).select_related("domain")
        )
        verification: dict[str, str] = {}
        dkim: dict[str, str] = {}
        verification_domains = [
            domain
            for domain in domains
            if domain.setup_mode == Domain.SetupMode.DIRECT_MX
            or domain.outbound_status != Domain.OutboundStatus.DISABLED
            or any(
                transition.domain_id == domain.id
                and transition.to_mode == Domain.SetupMode.DIRECT_MX
                for transition in transitions
            )
        ]
        dkim_domains = [
            domain for domain in domains if domain.outbound_status != Domain.OutboundStatus.DISABLED
        ]
        if verification_domains:
            try:
                ses = boto3.client("ses", region_name=settings.AWS_REGION)
                for start in range(0, len(verification_domains), 100):
                    identities = [
                        item.hostname for item in verification_domains[start : start + 100]
                    ]
                    verification.update(
                        {
                            key: str(value.get("VerificationStatus", "UNKNOWN"))
                            for key, value in ses.get_identity_verification_attributes(
                                Identities=identities
                            )
                            .get("VerificationAttributes", {})
                            .items()
                        }
                    )
                for start in range(0, len(dkim_domains), 100):
                    identities = [item.hostname for item in dkim_domains[start : start + 100]]
                    dkim.update(
                        {
                            key: str(value.get("DkimVerificationStatus", "UNKNOWN"))
                            for key, value in ses.get_identity_dkim_attributes(
                                Identities=identities
                            )
                            .get("DkimAttributes", {})
                            .items()
                        }
                    )
                for domain in verification_domains:
                    adoption_statuses = reconcile_ses_identity_adoption(
                        domain,
                        ses_verification_status=verification.get(domain.hostname, ""),
                        dkim_verification_status=dkim.get(domain.hostname, ""),
                        ses_client=ses,
                    )
                    if adoption_statuses is not None:
                        verification[domain.hostname], dkim[domain.hostname] = adoption_statuses
            except (BotoCoreError, ClientError):
                # DNS checks still provide useful, durable observations. Preserve
                # the previous outbound readiness until SES can be queried again.
                verification = {}
                dkim = {}
        for transition in transitions:
            try:
                refresh_routing_transition(
                    transition,
                    ses_verification_status=verification.get(transition.domain.hostname),
                    now=now,
                )
            except ValidationError:
                # A transient MX lookup problem must not disturb the active route.
                continue
        for domain in domains:
            previous_status = domain.status
            apply_domain_readiness(
                domain,
                ses_verification_status=verification.get(domain.hostname),
                dkim_verification_status=dkim.get(domain.hostname),
                now=now,
            )
            if (
                previous_status != Domain.Status.DEGRADED
                and domain.status == Domain.Status.DEGRADED
            ):
                create_domain_drift_notifications(domain)
        receipt_rule_reconciled = False
        if (
            settings.AWS_INGRESS_BUCKET
            and settings.AWS_INBOUND_TOPIC_ARN
            and (
                any(domain.setup_mode == Domain.SetupMode.DIRECT_MX for domain in domains)
                or any(
                    transition.to_mode == Domain.SetupMode.DIRECT_MX for transition in transitions
                )
                or InboundRoutingTransition.objects.filter(
                    domain__in=domains,
                    from_mode=Domain.SetupMode.DIRECT_MX,
                    status=InboundRoutingTransition.Status.GRACE,
                    grace_until__gt=now,
                ).exists()
            )
        ):
            # This is intentionally unconditional and idempotent. A previous AWS
            # failure must not strand an already-committed ownership transition,
            # and a fresh CDK rule set must receive the service-domain allowlist.
            reconcile_receipt_rule()
            receipt_rule_reconciled = True

        waiting_transitions = InboundRoutingTransition.objects.filter(
            domain_id__in=touched_domains,
            status=InboundRoutingTransition.Status.WAITING_TEST,
        ).select_related("domain")
        for transition in waiting_transitions:
            if transition.to_mode == Domain.SetupMode.DIRECT_MX and not receipt_rule_reconciled:
                continue
            try:
                test, _, created = ensure_routing_transition_test(
                    transition,
                    receipt_rule_reconciler=_noop_receipt_rule_reconciler,
                )
            except ValidationError:
                continue
            if created:
                AuditEvent.objects.create(
                    domain=transition.domain,
                    actor_type=AuditEvent.ActorType.SYSTEM,
                    event_type="domain.routing_transition_test_created",
                    object_type="DomainTest",
                    object_id=test.id,
                    request_id=f"dns:{transition.domain_id}:test:{test.id}",
                    metadata={
                        "routing_transition_id": str(transition.id),
                        "setup_generation": test.setup_generation,
                    },
                )

        transition_domain_ids = InboundRoutingTransition.objects.filter(
            domain_id__in=touched_domains,
            status__in=ACTIVE_TRANSITION_STATUSES,
        ).values_list("domain_id", flat=True)
        pending_test_domains = (
            Domain.objects.filter(
                id__in=touched_domains,
                status=Domain.Status.PENDING_TEST,
            )
            .exclude(id__in=transition_domain_ids)
            .order_by("hostname")
        )
        for domain in pending_test_domains:
            if domain.setup_mode == Domain.SetupMode.DIRECT_MX and not receipt_rule_reconciled:
                continue
            try:
                test, _, created = ensure_domain_test(
                    domain,
                    receipt_rule_reconciler=_noop_receipt_rule_reconciler,
                )
            except ValidationError:
                continue
            if created:
                AuditEvent.objects.create(
                    domain=domain,
                    actor_type=AuditEvent.ActorType.SYSTEM,
                    event_type="domain.test_created",
                    object_type="DomainTest",
                    object_id=test.id,
                    request_id=f"dns:{domain.id}:test:{test.id}",
                    metadata={"setup_generation": test.setup_generation},
                )
        self.stdout.write(
            json.dumps({"checked": checked, "invalid_required": invalid}, sort_keys=True)
        )
