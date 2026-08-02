from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from inbox.models import Domain, InboundRoutingTransition


class ReceiptRuleLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationResult:
    recipients: tuple[str, ...]
    action: str


def receipt_allowlist() -> tuple[str, ...]:
    now = timezone.now()
    direct_domains = (
        Domain.objects.filter(ownership_verified=True)
        .exclude(status=Domain.Status.DISABLED)
        .filter(
            Q(
                setup_mode=Domain.SetupMode.DIRECT_MX,
                status__in=(
                    Domain.Status.PENDING_TEST,
                    Domain.Status.READY,
                    Domain.Status.DEGRADED,
                ),
            )
            | Q(
                setup_mode=Domain.SetupMode.DIRECT_MX,
                routing_transitions__from_mode=Domain.SetupMode.DIRECT_MX,
                routing_transitions__status__in=(
                    InboundRoutingTransition.Status.PREPARING,
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                    InboundRoutingTransition.Status.FAILED,
                ),
            )
            | Q(
                routing_transitions__to_mode=Domain.SetupMode.DIRECT_MX,
                routing_transitions__status__in=(
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                    InboundRoutingTransition.Status.GRACE,
                ),
            )
            | Q(
                routing_transitions__from_mode=Domain.SetupMode.DIRECT_MX,
                routing_transitions__status=InboundRoutingTransition.Status.GRACE,
                routing_transitions__grace_until__gt=now,
            )
        )
        .values_list("hostname", flat=True)
        .distinct()
    )
    recipients = sorted({settings.INBOUND_SERVICE_DOMAIN, *direct_domains})
    if len(recipients) > 500:
        raise ReceiptRuleLimitError(
            "SES receipt rules accept at most 500 recipient conditions, "
            "including the service domain."
        )
    return tuple(recipients)


def _rule_payload(recipients: tuple[str, ...]) -> dict[str, Any]:
    return {
        "Name": settings.AWS_SES_RECEIPT_RULE,
        "Enabled": True,
        "TlsPolicy": "Require",
        "Recipients": list(recipients),
        "ScanEnabled": True,
        "Actions": [
            {
                "S3Action": {
                    "BucketName": settings.AWS_INGRESS_BUCKET,
                    "ObjectKeyPrefix": "ingress/",
                    "TopicArn": settings.AWS_INBOUND_TOPIC_ARN,
                }
            }
        ],
    }


def reconcile_receipt_rule(ses_client: Any | None = None) -> ReconciliationResult:
    if not settings.AWS_INGRESS_BUCKET or not settings.AWS_INBOUND_TOPIC_ARN:
        raise RuntimeError("AWS ingress bucket and inbound topic must be configured.")
    recipients = receipt_allowlist()
    client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
    rule = _rule_payload(recipients)
    try:
        client.describe_receipt_rule_set(RuleSetName=settings.AWS_SES_RECEIPT_RULE_SET)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "RuleSetDoesNotExist":
            raise RuntimeError("The CDK-managed SES receipt rule set does not exist.") from exc
        raise
    try:
        client.describe_receipt_rule(
            RuleSetName=settings.AWS_SES_RECEIPT_RULE_SET,
            RuleName=settings.AWS_SES_RECEIPT_RULE,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "RuleDoesNotExist",
            "ReceiptRuleDoesNotExist",
        }:
            raise
        client.create_receipt_rule(
            RuleSetName=settings.AWS_SES_RECEIPT_RULE_SET,
            Rule=rule,
        )
        return ReconciliationResult(recipients, "created")
    client.update_receipt_rule(
        RuleSetName=settings.AWS_SES_RECEIPT_RULE_SET,
        Rule=rule,
    )
    return ReconciliationResult(recipients, "updated")
