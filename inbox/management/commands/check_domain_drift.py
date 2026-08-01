import json

import boto3
import dns.exception
import dns.resolver
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from inbox.models import Domain, DomainDNSRecord
from inbox.services.domains import apply_domain_readiness, reconcile_ses_identity_adoption
from inbox.services.notifications import create_domain_drift_notifications
from inbox.services.receipt_rules import reconcile_receipt_rule


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


class Command(BaseCommand):
    help = "Check customer-managed DNS records and mark readiness drift without writing DNS."

    def handle(self, *args, **options):
        now = timezone.now()
        checked = invalid = 0
        touched_domains = set()
        for record in DomainDNSRecord.objects.filter(
            domain__status__in=[
                Domain.Status.PENDING_DNS,
                Domain.Status.PENDING_TEST,
                Domain.Status.READY,
                Domain.Status.DEGRADED,
            ]
        ).select_related("domain"):
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
            .prefetch_related("dns_records", "tests")
            .order_by("hostname")
        )
        verification: dict[str, str] = {}
        dkim: dict[str, str] = {}
        if domains:
            try:
                ses = boto3.client("ses", region_name=settings.AWS_REGION)
                for start in range(0, len(domains), 100):
                    identities = [item.hostname for item in domains[start : start + 100]]
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
                for domain in domains:
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
        if settings.AWS_INGRESS_BUCKET and settings.AWS_INBOUND_TOPIC_ARN:
            # This is intentionally unconditional and idempotent. A previous AWS
            # failure must not strand an already-committed ownership transition,
            # and a fresh CDK rule set must receive the service-domain allowlist.
            reconcile_receipt_rule()
        self.stdout.write(
            json.dumps({"checked": checked, "invalid_required": invalid}, sort_keys=True)
        )
