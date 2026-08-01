from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

from inbox.models import Domain


class ReceiptRuleLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReconciliationResult:
    recipients: tuple[str, ...]
    action: str


def receipt_allowlist() -> tuple[str, ...]:
    direct_domains = Domain.objects.filter(
        setup_mode=Domain.SetupMode.DIRECT_MX,
        ownership_verified=True,
        status__in=[
            Domain.Status.PENDING_TEST,
            Domain.Status.READY,
            Domain.Status.DEGRADED,
        ],
    ).values_list("hostname", flat=True)
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
