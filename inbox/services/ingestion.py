from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import boto3
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from inbox.models import (
    Attachment,
    AuditEvent,
    Conversation,
    DeliveryEvent,
    Domain,
    DomainTest,
    DraftApproval,
    InboundRoute,
    InboundRoutingTransition,
    IngressEvent,
    Message,
    MessageRecipient,
    MessageReference,
    OutboundMessage,
    ReplyDraft,
    RetentionPolicy,
)
from inbox.services.domains import apply_domain_readiness
from inbox.services.mime import MAX_MIME_BYTES, ParsedMIME, parse_mime
from inbox.services.notifications import create_security_notifications
from inbox.services.routing_transitions import finalize_routing_transition_test
from inbox.services.threading import (
    match_conversation,
    merge_suggestion,
    normalize_subject,
    reference_hash,
)

logger = logging.getLogger(__name__)
MESSAGE_NAMESPACE = uuid.UUID("2b7811a5-fdac-4bed-b05a-13f23fac6cf4")


class PermanentIngressError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RoutedDomain:
    domain: Domain
    recipients: tuple[str, ...]
    routes: tuple[InboundRoute, ...]


@dataclass(frozen=True)
class IngressEnvelope:
    sns_message_id: str
    topic_arn: str
    notification: dict[str, Any]
    payload_digest: str


def parse_sns_envelope(body: str) -> IngressEnvelope:
    try:
        outer = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PermanentIngressError("invalid_sqs_body", "SQS body is not valid JSON.") from exc
    if not isinstance(outer, dict) or outer.get("Type") != "Notification":
        raise PermanentIngressError(
            "invalid_sns_envelope", "Expected an SNS notification envelope."
        )
    topic_arn = str(outer.get("TopicArn", ""))
    allowed_topics = {settings.AWS_INBOUND_TOPIC_ARN, settings.AWS_DELIVERY_TOPIC_ARN} - {""}
    if topic_arn not in allowed_topics:
        raise PermanentIngressError(
            "unexpected_topic", "SNS notification came from an unexpected topic."
        )
    sns_message_id = str(outer.get("MessageId", ""))
    raw_message = outer.get("Message")
    if not sns_message_id or not isinstance(raw_message, str):
        raise PermanentIngressError(
            "invalid_sns_envelope", "SNS notification is missing required fields."
        )
    try:
        notification = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise PermanentIngressError(
            "invalid_ses_notification", "SES notification is not JSON."
        ) from exc
    if not isinstance(notification, dict):
        raise PermanentIngressError(
            "invalid_ses_notification", "SES notification must be an object."
        )
    return IngressEnvelope(
        sns_message_id=sns_message_id,
        topic_arn=topic_arn,
        notification=notification,
        payload_digest=hashlib.sha256(raw_message.encode()).hexdigest(),
    )


def quarantine_invalid_sqs_body(body: str, error: PermanentIngressError) -> IngressEvent:
    digest = hashlib.sha256(body.encode(errors="replace")).hexdigest()
    event, _ = IngressEvent.objects.get_or_create(
        sns_message_id=f"invalid:{digest}",
        defaults={
            "ses_message_id": "",
            "source_topic_arn": "",
            "source_bucket": "",
            "source_key": "",
            "payload_digest": digest,
            "status": IngressEvent.Status.QUARANTINED,
            "attempts": 1,
            "error_code": error.code[:64],
            "error_message": str(error)[:240],
            "processed_at": timezone.now(),
        },
    )
    return event


def _route_domains(recipients: list[str]) -> list[RoutedDomain]:
    normalized = {address.strip().casefold() for address in recipients if "@" in address}
    grouped_recipients: dict[uuid.UUID, set[str]] = defaultdict(set)
    grouped_routes: dict[uuid.UUID, dict[uuid.UUID, InboundRoute]] = defaultdict(dict)
    domains: dict[uuid.UUID, Domain] = {}

    now = timezone.now()
    forwarding_routes = (
        InboundRoute.objects.filter(
            address__in=normalized,
            is_active=True,
            kind=InboundRoute.Kind.FORWARDING_ALIAS,
        )
        .exclude(domain__status=Domain.Status.DISABLED)
        .filter(
            Q(
                domain__setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
                domain__status__in=(
                    Domain.Status.PENDING_TEST,
                    Domain.Status.READY,
                    Domain.Status.DEGRADED,
                ),
            )
            | Q(
                domain__setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
                domain__routing_transitions__from_mode=Domain.SetupMode.PROVIDER_FORWARD,
                domain__routing_transitions__status__in=(
                    InboundRoutingTransition.Status.PREPARING,
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                    InboundRoutingTransition.Status.FAILED,
                ),
            )
            | Q(
                routing_transition__to_mode=Domain.SetupMode.PROVIDER_FORWARD,
                routing_transition__status__in=(
                    InboundRoutingTransition.Status.PREPARING,
                    InboundRoutingTransition.Status.WAITING_DNS,
                    InboundRoutingTransition.Status.WAITING_TEST,
                    InboundRoutingTransition.Status.GRACE,
                ),
            )
            | Q(grace_until__gt=now)
        )
        .select_related("domain", "routing_transition")
        .distinct()
    )
    for route in forwarding_routes:
        domain = route.domain
        domains[domain.id] = domain
        grouped_recipients[domain.id].add(route.address.casefold())
        grouped_routes[domain.id][route.id] = route

    recipient_domains = {address.rsplit("@", 1)[1] for address in normalized}
    direct_domains = (
        Domain.objects.filter(
            hostname__in=recipient_domains,
            ownership_verified=True,
        )
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
        .select_related("owner")
        .distinct()
    )
    for domain in direct_domains:
        domains[domain.id] = domain
        grouped_recipients[domain.id].update(
            address for address in normalized if address.endswith(f"@{domain.hostname}")
        )

    return [
        RoutedDomain(
            domain=domains[domain_id],
            recipients=tuple(sorted(grouped_recipients[domain_id])),
            routes=tuple(grouped_routes[domain_id].values()),
        )
        for domain_id in sorted(domains, key=str)
    ]


def _verdict(receipt: dict[str, Any], name: str) -> str:
    value = receipt.get(name, {})
    status = str(value.get("status", "UNKNOWN")).upper() if isinstance(value, dict) else "UNKNOWN"
    return status if status in Message.Verdict.values else Message.Verdict.UNKNOWN


def _copy_objects(
    *,
    s3_client: Any,
    source_bucket: str,
    source_key: str,
    domain: Domain,
    message_id: uuid.UUID,
    parsed: ParsedMIME,
) -> tuple[str, list[tuple[str, int]]]:
    prefix = f"domains/{domain.id}/messages/{message_id}"
    raw_key = f"{prefix}/raw/message.eml"
    s3_client.copy_object(
        Bucket=settings.AWS_INGRESS_BUCKET,
        Key=raw_key,
        CopySource={"Bucket": source_bucket, "Key": source_key},
        MetadataDirective="COPY",
        ServerSideEncryption="AES256",
    )
    attachment_keys: list[tuple[str, int]] = []
    for index, attachment in enumerate(parsed.attachments, start=1):
        attachment_id = uuid.uuid5(message_id, f"{index}:{attachment.sha256}")
        key = f"{prefix}/attachments/{attachment_id}"
        s3_client.put_object(
            Bucket=settings.AWS_INGRESS_BUCKET,
            Key=key,
            Body=attachment.content,
            ContentType=attachment.content_type or "application/octet-stream",
            ServerSideEncryption="AES256",
        )
        attachment_keys.append((key, index - 1))
    return raw_key, attachment_keys


def _store_domain_message(
    *,
    routed: RoutedDomain,
    parsed: ParsedMIME,
    mail: dict[str, Any],
    receipt: dict[str, Any],
    raw_key: str,
    attachment_keys: list[tuple[str, int]],
) -> Message:
    domain = routed.domain
    provider_message_id = str(mail["messageId"])
    existing = Message.objects.filter(
        domain=domain, provider_message_id=provider_message_id
    ).first()
    if existing:
        return existing
    received_at = _parse_timestamp(mail.get("timestamp")) or timezone.now()
    conversation = match_conversation(
        domain=domain, parsed=parsed, envelope_recipients=list(routed.recipients)
    )
    if conversation is None:
        conversation = Conversation.objects.create(
            domain=domain,
            subject=parsed.subject,
            normalized_subject=normalize_subject(parsed.subject),
            status=(
                Conversation.Status.QUARANTINED
                if _verdict(receipt, "virusVerdict") != Message.Verdict.PASS
                else Conversation.Status.OPEN
            ),
            first_message_at=received_at,
            last_message_at=received_at,
            last_inbound_at=received_at,
        )
        suggestion = merge_suggestion(domain, parsed.subject, conversation)
        if suggestion:
            conversation.merge_suggestion = suggestion
            conversation.save(update_fields=("merge_suggestion", "updated_at"))
    else:
        conversation.last_message_at = max(conversation.last_message_at, received_at)
        conversation.last_inbound_at = received_at
        conversation.archived_at = None
        conversation.trashed_at = None
        if conversation.status in {
            Conversation.Status.RESOLVED,
            Conversation.Status.WAITING_EXTERNAL,
        }:
            conversation.status = Conversation.Status.OPEN
            conversation.resolved_at = None
        conversation.save(
            update_fields=(
                "last_message_at",
                "last_inbound_at",
                "archived_at",
                "trashed_at",
                "status",
                "resolved_at",
                "updated_at",
            )
        )
        ReplyDraft.objects.filter(conversation=conversation, is_stale=False).update(is_stale=True)
        DraftApproval.objects.filter(
            revision__draft__conversation=conversation,
            invalidated_at__isnull=True,
        ).update(
            invalidated_at=timezone.now(),
            invalidated_reason="new_inbound_message",
        )

    virus_verdict = _verdict(receipt, "virusVerdict")
    spam_verdict = _verdict(receipt, "spamVerdict")
    message_id = uuid.uuid5(MESSAGE_NAMESPACE, f"{domain.id}:{provider_message_id}")
    message = Message.objects.create(
        id=message_id,
        domain=domain,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        provider_message_id=provider_message_id,
        rfc_message_id=parsed.rfc_message_id,
        from_address=parsed.from_address,
        reply_to_address=parsed.reply_to_address,
        subject=parsed.subject,
        text_body=parsed.text_body,
        html_body=parsed.html_body,
        sent_at=parsed.sent_at,
        received_at=received_at,
        spam_verdict=spam_verdict,
        virus_verdict=virus_verdict,
        dkim_verdict=_verdict(receipt, "dkimVerdict"),
        spf_verdict=_verdict(receipt, "spfVerdict"),
        dmarc_verdict=_verdict(receipt, "dmarcVerdict"),
        is_suspicious=(
            spam_verdict != Message.Verdict.PASS
            or _verdict(receipt, "dkimVerdict") == Message.Verdict.FAIL
            or _verdict(receipt, "spfVerdict") == Message.Verdict.FAIL
            or _verdict(receipt, "dmarcVerdict") == Message.Verdict.FAIL
        ),
        is_quarantined=virus_verdict != Message.Verdict.PASS,
        raw_s3_key=raw_key,
        raw_sha256=parsed.raw_sha256,
    )
    recipient_rows: dict[tuple[str, str], MessageRecipient] = {}
    for address in routed.recipients:
        recipient_rows[(MessageRecipient.Kind.ENVELOPE, address)] = MessageRecipient(
            domain=domain,
            message=message,
            kind=MessageRecipient.Kind.ENVELOPE,
            address=address,
            is_routing_recipient=True,
        )
    for kind, values in (
        (MessageRecipient.Kind.TO, parsed.to_addresses),
        (MessageRecipient.Kind.CC, parsed.cc_addresses),
    ):
        for address in values:
            recipient_rows.setdefault(
                (kind, address),
                MessageRecipient(
                    domain=domain,
                    message=message,
                    kind=kind,
                    address=address,
                ),
            )
    MessageRecipient.objects.bulk_create(recipient_rows.values(), ignore_conflicts=True)

    reference_rows: list[MessageReference] = []
    if parsed.rfc_message_id:
        reference_rows.append(
            MessageReference(
                domain=domain,
                message=message,
                kind=MessageReference.Kind.MESSAGE_ID,
                position=0,
                value_hash=reference_hash(parsed.rfc_message_id),
            )
        )
    reference_rows.extend(
        MessageReference(
            domain=domain,
            message=message,
            kind=MessageReference.Kind.REFERENCE,
            position=index,
            value_hash=reference_hash(value),
        )
        for index, value in enumerate(parsed.references)
    )
    reference_rows.extend(
        MessageReference(
            domain=domain,
            message=message,
            kind=MessageReference.Kind.IN_REPLY_TO,
            position=index,
            value_hash=reference_hash(value),
        )
        for index, value in enumerate(parsed.in_reply_to)
    )
    MessageReference.objects.bulk_create(reference_rows, ignore_conflicts=True)

    retention, _ = RetentionPolicy.objects.get_or_create(domain=domain)
    purge_at = received_at + timedelta(days=retention.attachment_days)
    Attachment.objects.bulk_create(
        [
            Attachment(
                domain=domain,
                message=message,
                display_name=parsed.attachments[index].filename,
                content_type=parsed.attachments[index].content_type,
                size=len(parsed.attachments[index].content),
                sha256=parsed.attachments[index].sha256,
                s3_key=key,
                scan_status=(
                    Attachment.ScanStatus.CLEAN
                    if virus_verdict == Message.Verdict.PASS
                    else Attachment.ScanStatus.QUARANTINED
                ),
                purge_at=purge_at,
            )
            for key, index in attachment_keys
        ]
    )
    candidate_addresses = {
        address.casefold() for address in (*parsed.to_addresses, *parsed.cc_addresses)
    }
    arrival_kind = (
        InboundRoute.Kind.FORWARDING_ALIAS
        if any(route.kind == InboundRoute.Kind.FORWARDING_ALIAS for route in routed.routes)
        else InboundRoute.Kind.DIRECT_DOMAIN
    )
    for test in DomainTest.objects.filter(
        domain=domain,
        status=DomainTest.Status.PENDING,
        expires_at__gt=timezone.now(),
    ).select_related("domain"):
        expected_address = (test.address or "").casefold()
        matching = next(
            (
                address
                for address in candidate_addresses
                if expected_address
                and secrets.compare_digest(address, expected_address)
                and address.endswith(f"@{test.domain.hostname}")
                and address.rsplit("@", 1)[0].startswith("test-")
                and secrets.compare_digest(
                    hashlib.sha256(
                        address.rsplit("@", 1)[0].removeprefix("test-").encode()
                    ).hexdigest(),
                    test.token_hash,
                )
            ),
            None,
        )
        if matching:
            if test.routing_transition_id:
                if not finalize_routing_transition_test(test, message, arrival_kind):
                    continue
                event_type = "domain.routing_transition_test_received"
            else:
                if not _finalize_domain_test(test, message, arrival_kind):
                    continue
                event_type = "domain.test_received"
            AuditEvent.objects.create(
                domain=domain,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type=event_type,
                object_type="DomainTest",
                object_id=test.id,
                request_id=f"ingress:{message.id}",
                metadata={
                    "arrival_kind": arrival_kind,
                    "setup_generation": test.setup_generation,
                },
            )
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.AWS,
        event_type="message.ingested",
        object_type="Message",
        object_id=message.id,
        request_id=f"ingress:{message.id}",
        metadata={
            "spam_verdict": message.spam_verdict,
            "virus_verdict": message.virus_verdict,
            "suspicious": message.is_suspicious,
            "quarantined": message.is_quarantined,
        },
    )
    create_security_notifications(message)
    return message


def _finalize_domain_test(
    test: DomainTest,
    message: Message,
    arrival_kind: str,
) -> bool:
    """Consume an initial delivery challenge once, only for its current route."""

    now = timezone.now()
    with transaction.atomic():
        locked_test = DomainTest.objects.select_for_update().get(id=test.id)
        if (
            locked_test.routing_transition_id is not None
            or locked_test.status != DomainTest.Status.PENDING
            or locked_test.expires_at <= now
            or locked_test.expected_route_kind != arrival_kind
            or locked_test.received_message_id is not None
            or message.domain_id != locked_test.domain_id
        ):
            return False

        locked_domain = Domain.objects.select_for_update().get(id=locked_test.domain_id)
        if (
            locked_domain.status != Domain.Status.PENDING_TEST
            or locked_test.setup_generation != locked_domain.inbound_setup_generation
            or locked_test.expected_setup_mode != locked_domain.setup_mode
            or not locked_domain.inbound_routes.filter(
                is_active=True,
                setup_generation=locked_test.setup_generation,
                kind=locked_test.expected_route_kind,
            ).exists()
        ):
            return False

        locked_test.status = DomainTest.Status.RECEIVED
        locked_test.received_message = message
        locked_test.save(update_fields=("status", "received_message", "updated_at"))
        apply_domain_readiness(locked_domain, now=now)
    return True


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.get_current_timezone())


def process_received_notification(
    envelope: IngressEnvelope, *, s3_client: Any | None = None
) -> bool:
    notification = envelope.notification
    mail = notification.get("mail")
    receipt = notification.get("receipt")
    if not isinstance(mail, dict) or not isinstance(receipt, dict) or not mail.get("messageId"):
        raise PermanentIngressError(
            "invalid_received_notification", "Missing SES mail or receipt fields."
        )
    action = receipt.get("action")
    recipients = receipt.get("recipients")
    if not isinstance(action, dict) or action.get("type") != "S3":
        raise PermanentIngressError("invalid_s3_action", "SES receipt action is not S3.")
    if not isinstance(recipients, list) or not all(isinstance(item, str) for item in recipients):
        raise PermanentIngressError("invalid_recipients", "SES receipt recipients are missing.")
    source_bucket = str(action.get("bucketName", ""))
    source_key = str(action.get("objectKey", ""))
    if (
        source_bucket != settings.AWS_INGRESS_BUCKET
        or not source_key.startswith("ingress/")
        or ".." in source_key
    ):
        raise PermanentIngressError(
            "unexpected_s3_object", "SES notification referenced an unexpected object."
        )

    event, created = IngressEvent.objects.get_or_create(
        sns_message_id=envelope.sns_message_id,
        defaults={
            "ses_message_id": str(mail["messageId"]),
            "source_topic_arn": envelope.topic_arn,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "payload_digest": envelope.payload_digest,
        },
    )
    if not created and event.payload_digest != envelope.payload_digest:
        event.status = IngressEvent.Status.QUARANTINED
        event.error_code = "sns_id_payload_mismatch"
        event.error_message = "A duplicate SNS ID carried a different payload digest."
        event.save(update_fields=("status", "error_code", "error_message", "updated_at"))
        return True
    if event.status == IngressEvent.Status.PROCESSED:
        return True
    event.status = IngressEvent.Status.PROCESSING
    event.attempts += 1
    event.save(update_fields=("status", "attempts", "updated_at"))

    routed_domains = _route_domains(recipients)
    if not routed_domains:
        event.status = IngressEvent.Status.QUARANTINED
        event.error_code = "unroutable_recipient"
        event.error_message = "No active tenant route matched the SES envelope recipients."
        event.processed_at = timezone.now()
        event.save(
            update_fields=("status", "error_code", "error_message", "processed_at", "updated_at")
        )
        return True

    s3 = s3_client or boto3.client("s3", region_name=settings.AWS_REGION)
    response = s3.get_object(Bucket=source_bucket, Key=source_key)
    raw = response["Body"].read(MAX_MIME_BYTES + 1)
    try:
        parsed = parse_mime(raw)
    except ValidationError as exc:
        event.status = IngressEvent.Status.QUARANTINED
        event.error_code = "malformed_mime"
        event.error_message = str(exc)[:240]
        event.processed_at = timezone.now()
        event.save(
            update_fields=("status", "error_code", "error_message", "processed_at", "updated_at")
        )
        return True

    for routed in routed_domains:
        message_id = uuid.uuid5(MESSAGE_NAMESPACE, f"{routed.domain.id}:{mail['messageId']}")
        raw_key, attachment_keys = _copy_objects(
            s3_client=s3,
            source_bucket=source_bucket,
            source_key=source_key,
            domain=routed.domain,
            message_id=message_id,
            parsed=parsed,
        )
        try:
            with transaction.atomic():
                _store_domain_message(
                    routed=routed,
                    parsed=parsed,
                    mail=mail,
                    receipt=receipt,
                    raw_key=raw_key,
                    attachment_keys=attachment_keys,
                )
        except IntegrityError:
            if not Message.objects.filter(
                domain=routed.domain, provider_message_id=str(mail["messageId"])
            ).exists():
                raise

    event.status = IngressEvent.Status.PROCESSED
    event.processed_at = timezone.now()
    event.error_code = ""
    event.error_message = ""
    event.save(
        update_fields=(
            "status",
            "processed_at",
            "error_code",
            "error_message",
            "updated_at",
        )
    )
    return True


DELIVERY_STATE_MAP = {
    "Delivery": OutboundMessage.Status.DELIVERED,
    "Bounce": OutboundMessage.Status.BOUNCED,
    "Complaint": OutboundMessage.Status.COMPLAINED,
    "Reject": OutboundMessage.Status.FAILED,
    "Rendering Failure": OutboundMessage.Status.FAILED,
}


def process_delivery_notification(envelope: IngressEnvelope) -> bool:
    notification = envelope.notification
    mail = notification.get("mail", {})
    if not isinstance(mail, dict):
        return True
    provider_message_id = str(mail.get("messageId", ""))
    outbound = OutboundMessage.objects.filter(provider_message_id=provider_message_id).first()
    if outbound is None:
        tags = mail.get("tags", {})
        tagged_id = tags.get("outbound_id") if isinstance(tags, dict) else None
        if isinstance(tagged_id, list):
            tagged_id = tagged_id[0] if tagged_id else None
        if isinstance(tagged_id, str):
            try:
                outbound = OutboundMessage.objects.filter(id=uuid.UUID(tagged_id)).first()
            except ValueError:
                outbound = None
            if outbound is not None and not outbound.provider_message_id:
                outbound.provider_message_id = provider_message_id
                outbound.save(update_fields=("provider_message_id", "updated_at"))
    if outbound is None:
        # Keep the SQS message visible for retry; SES delivery events can arrive before
        # the local ACCEPTED transaction becomes observable.
        return False
    event_type = str(
        notification.get("notificationType") or notification.get("eventType") or "Unknown"
    )
    occurred_at = _parse_timestamp(mail.get("timestamp")) or timezone.now()
    event, created = DeliveryEvent.objects.get_or_create(
        provider_event_id=envelope.sns_message_id,
        defaults={
            "domain": outbound.domain,
            "outbound_message": outbound,
            "provider_message_id": provider_message_id,
            "event_type": event_type,
            "metadata": {"source": "ses"},
            "occurred_at": occurred_at,
        },
    )
    next_state = DELIVERY_STATE_MAP.get(event_type)
    if next_state and (
        outbound.status == OutboundMessage.Status.COMPLAINED
        or (
            outbound.status == OutboundMessage.Status.BOUNCED
            and next_state != OutboundMessage.Status.COMPLAINED
        )
        or (
            next_state == OutboundMessage.Status.DELIVERED
            and outbound.status == OutboundMessage.Status.FAILED
        )
        or (
            next_state == OutboundMessage.Status.FAILED
            and outbound.status == OutboundMessage.Status.DELIVERED
        )
    ):
        next_state = None
    if next_state:
        outbound.status = next_state
        fields = ["status", "updated_at"]
        if next_state == OutboundMessage.Status.DELIVERED:
            outbound.delivered_at = occurred_at
            fields.append("delivered_at")
        elif next_state in {
            OutboundMessage.Status.FAILED,
            OutboundMessage.Status.BOUNCED,
            OutboundMessage.Status.COMPLAINED,
        }:
            outbound.failed_at = occurred_at
            fields.append("failed_at")
        outbound.save(update_fields=fields)
    if created:
        AuditEvent.objects.create(
            domain=outbound.domain,
            actor_type=AuditEvent.ActorType.AWS,
            event_type="outbound.delivery_event",
            object_type="OutboundMessage",
            object_id=outbound.id,
            request_id=f"delivery:{event.id}",
            metadata={"event_type": event_type, "status": outbound.status},
        )
    return True


def process_sqs_body(body: str, *, s3_client: Any | None = None) -> bool:
    envelope = parse_sns_envelope(body)
    notification_type = str(
        envelope.notification.get("notificationType")
        or envelope.notification.get("eventType")
        or ""
    )
    if notification_type == "Received":
        return process_received_notification(envelope, s3_client=s3_client)
    return process_delivery_notification(envelope)


def consume_queue(*, max_runtime: int = 55, sqs_client: Any | None = None) -> dict[str, int]:
    if not settings.AWS_INGRESS_QUEUE_URL:
        raise RuntimeError("AWS_INGRESS_QUEUE_URL is not configured.")
    sqs = sqs_client or boto3.client("sqs", region_name=settings.AWS_REGION)
    deadline = time.monotonic() + max(1, max_runtime)
    counts = {"received": 0, "deleted": 0, "retry": 0, "quarantined": 0}
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        wait_seconds = max(0, min(20, int(remaining)))
        response = sqs.receive_message(
            QueueUrl=settings.AWS_INGRESS_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=300,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if not messages and wait_seconds == 0:
            break
        for message in messages:
            counts["received"] += 1
            try:
                should_delete = process_sqs_body(str(message.get("Body", "")))
            except PermanentIngressError as exc:
                logger.warning("permanent_ingress_error code=%s", exc.code)
                quarantine_invalid_sqs_body(str(message.get("Body", "")), exc)
                should_delete = True
                counts["quarantined"] += 1
            except Exception:
                logger.exception("transient_ingress_error")
                counts["retry"] += 1
                should_delete = False
            if should_delete:
                sqs.delete_message(
                    QueueUrl=settings.AWS_INGRESS_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                )
                counts["deleted"] += 1
        if time.monotonic() >= deadline:
            break
    return counts
