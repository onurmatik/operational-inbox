from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import boto3
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DomainTest,
    DurableJob,
    InboundRoute,
    InboundRoutingTransition,
    Message,
)
from inbox.services.domains import (
    MXLayout,
    _upsert_dns_instruction,
    application_ownership_record_name,
    classify_mx_layout,
    expected_inbound_mx_exchange,
    inspect_mx,
)

ACTIVE_TRANSITION_STATUSES = (
    InboundRoutingTransition.Status.PREPARING,
    InboundRoutingTransition.Status.WAITING_DNS,
    InboundRoutingTransition.Status.WAITING_TEST,
    InboundRoutingTransition.Status.GRACE,
    InboundRoutingTransition.Status.FAILED,
)

CANCELLABLE_TRANSITION_STATUSES = (
    InboundRoutingTransition.Status.PREPARING,
    InboundRoutingTransition.Status.WAITING_DNS,
    InboundRoutingTransition.Status.WAITING_TEST,
    InboundRoutingTransition.Status.FAILED,
)

ACCEPTING_TARGET_STATUSES = (
    InboundRoutingTransition.Status.WAITING_DNS,
    InboundRoutingTransition.Status.WAITING_TEST,
    InboundRoutingTransition.Status.GRACE,
)


def routing_grace_period() -> timedelta:
    return timedelta(hours=getattr(settings, "ROUTING_TRANSITION_GRACE_HOURS", 24))


def route_kind_for_mode(setup_mode: str) -> str:
    return (
        InboundRoute.Kind.DIRECT_DOMAIN
        if setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )


def active_routing_transition(domain: Domain) -> InboundRoutingTransition | None:
    return (
        domain.routing_transitions.filter(status__in=ACTIVE_TRANSITION_STATUSES)
        .order_by("-generation")
        .first()
    )


def _next_transition_generation(domain: Domain) -> int:
    latest = domain.routing_transitions.aggregate(value=Max("generation"))["value"] or 0
    return max(domain.inbound_setup_generation, latest) + 1


def begin_routing_transition(
    domain: Domain,
    target_mode: str,
) -> tuple[InboundRoutingTransition, bool]:
    """Prepare an opposite receiving route without changing the active route."""

    if target_mode not in Domain.SetupMode.values:
        raise ValidationError("Select a supported receiving route.")

    with transaction.atomic():
        locked_domain = Domain.objects.select_for_update().get(id=domain.id)
        if locked_domain.status == Domain.Status.DISABLED:
            raise ValidationError("A disabled domain cannot change its receiving route.")

        current = (
            InboundRoutingTransition.objects.select_for_update()
            .filter(domain=locked_domain, status__in=ACTIVE_TRANSITION_STATUSES)
            .order_by("-generation")
            .first()
        )
        if current is not None:
            if current.to_mode == target_mode:
                return current, False
            if current.status != InboundRoutingTransition.Status.GRACE:
                raise ValidationError("Cancel the current receiving-route transition first.")

            now = timezone.now()
            target_route_ids = current.routes.values_list("id", flat=True)
            locked_domain.inbound_routes.filter(
                is_active=True,
                kind=route_kind_for_mode(current.from_mode),
                grace_until=current.grace_until,
            ).exclude(id__in=target_route_ids).update(is_active=False, updated_at=now)
            current.status = InboundRoutingTransition.Status.COMPLETE
            current.completed_at = now
            current.save(update_fields=("status", "completed_at", "updated_at"))
            AuditEvent.objects.create(
                domain=locked_domain,
                actor_type=AuditEvent.ActorType.SYSTEM,
                event_type="domain.receiving_route_grace_ended_for_reverse",
                object_type="InboundRoutingTransition",
                object_id=current.id,
                request_id=f"routing-transition:{current.id}:reverse",
                metadata={
                    "from": current.from_mode,
                    "to": current.to_mode,
                    "generation": current.generation,
                },
            )
        if target_mode == locked_domain.setup_mode:
            raise ValidationError("This receiving route is already active.")

        transition = InboundRoutingTransition.objects.create(
            domain=locked_domain,
            generation=_next_transition_generation(locked_domain),
            from_mode=locked_domain.setup_mode,
            to_mode=target_mode,
            from_domain_status=locked_domain.status,
        )
        local_part = f"route-{secrets.token_urlsafe(24).lower()}"
        InboundRoute.objects.create(
            domain=locked_domain,
            routing_transition=transition,
            setup_generation=transition.generation,
            kind=route_kind_for_mode(target_mode),
            local_part=local_part,
            address=f"{local_part}@{settings.INBOUND_SERVICE_DOMAIN}",
            is_active=True,
        )
        DomainTest.objects.filter(
            domain=locked_domain,
            status=DomainTest.Status.PENDING,
        ).update(status=DomainTest.Status.EXPIRED, updated_at=timezone.now())
        _enqueue_job(
            domain=locked_domain,
            kind="provision_routing_transition",
            idempotency_key=(
                f"provision-routing-transition:{transition.id}:{transition.generation}"
            ),
            payload={
                "transition_id": str(transition.id),
                "generation": transition.generation,
            },
        )
        return transition, True


def _enqueue_job(
    *,
    domain: Domain,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    due_at=None,
) -> DurableJob:
    job, _ = DurableJob.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "domain": domain,
            "kind": kind,
            "payload": payload,
            "due_at": due_at or timezone.now(),
        },
    )
    return job


def cancel_routing_transition(transition: InboundRoutingTransition) -> bool:
    """Cancel an uncut transition and leave the active route untouched."""

    with transaction.atomic():
        locked = (
            InboundRoutingTransition.objects.select_for_update()
            .select_related("domain")
            .get(id=transition.id)
        )
        if locked.status == InboundRoutingTransition.Status.CANCELLED:
            return False
        if locked.status not in CANCELLABLE_TRANSITION_STATUSES:
            raise ValidationError("This receiving-route transition can no longer be cancelled.")
        now = timezone.now()
        locked.status = InboundRoutingTransition.Status.CANCELLED
        locked.cancelled_at = now
        locked.error_code = ""
        locked.error_message = ""
        locked.save(
            update_fields=(
                "status",
                "cancelled_at",
                "error_code",
                "error_message",
                "updated_at",
            )
        )
        locked.routes.update(is_active=False, grace_until=None, updated_at=now)
        locked.tests.filter(status=DomainTest.Status.PENDING).update(
            status=DomainTest.Status.EXPIRED,
            updated_at=now,
        )
        if locked.from_domain_status == Domain.Status.PROVISIONING:
            _enqueue_job(
                domain=locked.domain,
                kind="provision_domain",
                idempotency_key=(
                    f"provision-domain:{locked.domain_id}:resume-after-transition:{locked.id}"
                ),
                payload={
                    "domain_id": str(locked.domain_id),
                    "setup_generation": locked.domain.inbound_setup_generation,
                    "setup_mode": locked.domain.setup_mode,
                },
            )
        if locked.to_mode == Domain.SetupMode.DIRECT_MX:
            locked.domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.MX).delete()
            if locked.domain.outbound_status == Domain.OutboundStatus.DISABLED:
                locked.domain.dns_records.filter(
                    purpose=DomainDNSRecord.Purpose.SES_VERIFICATION
                ).delete()
            _enqueue_job(
                domain=locked.domain,
                kind="reconcile_receipt_rule",
                idempotency_key=f"receipt-rule:transition-cancel:{locked.id}",
                payload={"domain_id": str(locked.domain_id)},
            )
        return True


def _current_ownership_token(domain: Domain) -> str:
    return domain.dns_records.filter(
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        name=application_ownership_record_name(domain),
    ).values_list("value", flat=True).first() or secrets.token_urlsafe(32)


def provision_routing_transition(
    transition: InboundRoutingTransition,
    *,
    ses_client=None,
    receipt_rule_reconciler: Callable[[], object] | None = None,
) -> InboundRoutingTransition:
    """Create target instructions while the domain's active receiving path stays live."""

    transition = InboundRoutingTransition.objects.select_related("domain").get(id=transition.id)
    if transition.status == InboundRoutingTransition.Status.WAITING_DNS:
        if (
            transition.to_mode == Domain.SetupMode.DIRECT_MX
            and settings.AWS_INGRESS_BUCKET
            and settings.AWS_INBOUND_TOPIC_ARN
        ):
            if receipt_rule_reconciler is None:
                from inbox.services.receipt_rules import reconcile_receipt_rule

                receipt_rule_reconciler = reconcile_receipt_rule
            receipt_rule_reconciler()
        return transition
    if transition.status not in {
        InboundRoutingTransition.Status.PREPARING,
        InboundRoutingTransition.Status.FAILED,
    }:
        return transition
    domain = transition.domain
    if domain.status == Domain.Status.DISABLED:
        return transition

    ownership_token = _current_ownership_token(domain)
    verification_token = ""
    verification_status = domain.ses_identity_status
    identity_origin = domain.ses_identity_origin

    if transition.to_mode == Domain.SetupMode.DIRECT_MX:
        client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)
        attributes = client.get_identity_verification_attributes(Identities=[domain.hostname]).get(
            "VerificationAttributes", {}
        )
        identity_attributes = attributes.get(domain.hostname, {})
        identity_exists = domain.hostname in attributes
        verification_token = str(identity_attributes.get("VerificationToken", ""))
        verification_status = str(identity_attributes.get("VerificationStatus", "")).upper()
        has_fresh_ownership = domain.dns_records.filter(
            purpose=DomainDNSRecord.Purpose.OWNERSHIP,
            name=application_ownership_record_name(domain),
            status=DomainDNSRecord.Status.VALID,
        ).exists()
        may_manage_identity = (
            identity_origin
            in {
                Domain.SESIdentityOrigin.MANAGED,
                Domain.SESIdentityOrigin.ADOPTED,
            }
            or has_fresh_ownership
        )

        if not identity_exists:
            verification = client.verify_domain_identity(Domain=domain.hostname)
            verification_token = str(verification["VerificationToken"])
            verification_status = "PENDING"
            identity_origin = Domain.SESIdentityOrigin.MANAGED
        else:
            if not identity_origin:
                identity_origin = (
                    Domain.SESIdentityOrigin.ADOPTED
                    if has_fresh_ownership
                    else Domain.SESIdentityOrigin.ADOPTION_PENDING
                )
            if may_manage_identity and (
                verification_status not in {"PENDING", "SUCCESS"}
                or (verification_status == "PENDING" and not verification_token)
            ):
                verification = client.verify_domain_identity(Domain=domain.hostname)
                verification_token = str(verification["VerificationToken"])
                verification_status = "PENDING"

    with transaction.atomic():
        locked = (
            InboundRoutingTransition.objects.select_for_update()
            .select_related("domain")
            .get(id=transition.id)
        )
        if (
            locked.status
            not in {
                InboundRoutingTransition.Status.PREPARING,
                InboundRoutingTransition.Status.FAILED,
            }
            or locked.domain.status == Domain.Status.DISABLED
        ):
            return locked

        _upsert_dns_instruction(
            locked.domain,
            purpose=DomainDNSRecord.Purpose.OWNERSHIP,
            record_type="TXT",
            name=application_ownership_record_name(locked.domain),
            value=ownership_token,
            is_required=True,
        )
        if locked.to_mode == Domain.SetupMode.DIRECT_MX:
            if verification_token:
                _upsert_dns_instruction(
                    locked.domain,
                    purpose=DomainDNSRecord.Purpose.SES_VERIFICATION,
                    record_type="TXT",
                    name=f"_amazonses.{locked.domain.hostname}",
                    value=verification_token,
                    is_required=locked.domain.setup_mode == Domain.SetupMode.DIRECT_MX,
                )
            _upsert_dns_instruction(
                locked.domain,
                purpose=DomainDNSRecord.Purpose.MX,
                record_type="MX",
                name=locked.domain.hostname,
                value=expected_inbound_mx_exchange(),
                priority=10,
                is_required=locked.domain.setup_mode == Domain.SetupMode.DIRECT_MX,
            )
            locked.domain.ses_identity_status = verification_status or "PENDING"
            locked.domain.ses_identity_origin = identity_origin
            locked.domain.save(
                update_fields=("ses_identity_status", "ses_identity_origin", "updated_at")
            )
        locked.status = InboundRoutingTransition.Status.WAITING_DNS
        locked.error_code = ""
        locked.error_message = ""
        locked.save(update_fields=("status", "error_code", "error_message", "updated_at"))
        transition = locked

    if (
        transition.to_mode == Domain.SetupMode.DIRECT_MX
        and settings.AWS_INGRESS_BUCKET
        and settings.AWS_INBOUND_TOPIC_ARN
    ):
        if receipt_rule_reconciler is None:
            from inbox.services.receipt_rules import reconcile_receipt_rule

            receipt_rule_reconciler = reconcile_receipt_rule
        receipt_rule_reconciler()
    return transition


def refresh_routing_transition(
    transition: InboundRoutingTransition,
    *,
    resolver=None,
    ses_verification_status: str | None = None,
    now=None,
) -> InboundRoutingTransition:
    """Advance target DNS readiness without mutating the active domain health."""

    now = now or timezone.now()
    transition = InboundRoutingTransition.objects.select_related("domain").get(id=transition.id)
    if transition.status not in {
        InboundRoutingTransition.Status.WAITING_DNS,
        InboundRoutingTransition.Status.WAITING_TEST,
    }:
        return transition

    domain = transition.domain
    ownership_valid = domain.dns_records.filter(
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        name=application_ownership_record_name(domain),
        status=DomainDNSRecord.Status.VALID,
    ).exists()
    route_ready = transition.routes.filter(
        is_active=True,
        setup_generation=transition.generation,
        kind=route_kind_for_mode(transition.to_mode),
    ).exists()

    if transition.to_mode == Domain.SetupMode.DIRECT_MX:
        mx_valid = domain.dns_records.filter(
            purpose=DomainDNSRecord.Purpose.MX,
            record_type="MX",
            name=domain.hostname,
            value=expected_inbound_mx_exchange(),
            status=DomainDNSRecord.Status.VALID,
        ).exists()
        mx_exclusive = (
            classify_mx_layout(inspect_mx(domain.hostname, resolver=resolver))
            == MXLayout.OPERATIONAL_INBOX
        )
        ses_ready = str(ses_verification_status or domain.ses_identity_status).upper() == "SUCCESS"
        target_ready = ownership_valid and route_ready and mx_valid and mx_exclusive and ses_ready
    else:
        target_ready = (
            ownership_valid
            and route_ready
            and classify_mx_layout(inspect_mx(domain.hostname, resolver=resolver))
            == MXLayout.EXTERNAL
        )

    with transaction.atomic():
        locked = InboundRoutingTransition.objects.select_for_update().get(id=transition.id)
        if (
            locked.status
            not in {
                InboundRoutingTransition.Status.WAITING_DNS,
                InboundRoutingTransition.Status.WAITING_TEST,
            }
            or Domain.objects.filter(
                id=locked.domain_id,
                status=Domain.Status.DISABLED,
            ).exists()
        ):
            return locked
        if ownership_valid:
            ownership_updates: dict[str, Any] = {
                "ownership_verified": True,
                "updated_at": now,
            }
            if domain.verified_at is None:
                ownership_updates["verified_at"] = now
            Domain.objects.filter(id=locked.domain_id).update(**ownership_updates)
        if target_ready:
            locked.status = InboundRoutingTransition.Status.WAITING_TEST
            locked.dns_verified_at = locked.dns_verified_at or now
            locked.error_code = ""
            locked.error_message = ""
        else:
            if locked.status == InboundRoutingTransition.Status.WAITING_TEST:
                locked.tests.filter(status=DomainTest.Status.PENDING).update(
                    status=DomainTest.Status.EXPIRED,
                    updated_at=now,
                )
            locked.status = InboundRoutingTransition.Status.WAITING_DNS
            locked.dns_verified_at = None
        locked.save(
            update_fields=(
                "status",
                "dns_verified_at",
                "error_code",
                "error_message",
                "updated_at",
            )
        )
        return locked


def create_routing_transition_test(
    transition: InboundRoutingTransition,
    *,
    receipt_rule_reconciler: Callable[[], object] | None = None,
) -> tuple[DomainTest, str]:
    transition = InboundRoutingTransition.objects.select_related("domain").get(id=transition.id)
    if transition.status != InboundRoutingTransition.Status.WAITING_TEST:
        raise ValidationError(
            "Verify the target receiving route before generating its test address."
        )
    if transition.to_mode == Domain.SetupMode.DIRECT_MX:
        if receipt_rule_reconciler is None:
            from inbox.services.receipt_rules import reconcile_receipt_rule

            receipt_rule_reconciler = reconcile_receipt_rule
        receipt_rule_reconciler()

    raw = secrets.token_urlsafe(24).lower()
    now = timezone.now()
    with transaction.atomic():
        locked = (
            InboundRoutingTransition.objects.select_for_update()
            .select_related("domain")
            .get(id=transition.id)
        )
        if locked.status != InboundRoutingTransition.Status.WAITING_TEST:
            raise ValidationError(
                "Verify the target receiving route before generating its test address."
            )
        cooldown_started_at = now - timedelta(seconds=settings.DOMAIN_TEST_COOLDOWN_SECONDS)
        if locked.tests.filter(created_at__gte=cooldown_started_at).exists():
            raise ValidationError(
                "A target-route test address was generated recently. Use it or wait a minute."
            )
        test = DomainTest.objects.create(
            domain=locked.domain,
            routing_transition=locked,
            setup_generation=locked.generation,
            expected_setup_mode=locked.to_mode,
            expected_route_kind=route_kind_for_mode(locked.to_mode),
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=now + timedelta(hours=24),
        )
    return test, f"test-{raw}@{locked.domain.hostname}"


def finalize_routing_transition_test(
    test: DomainTest,
    message: Message,
    arrival_kind: str,
) -> bool:
    """Cut over only when a fresh test arrived through the candidate path."""

    if arrival_kind != test.expected_route_kind:
        return False
    now = timezone.now()
    with transaction.atomic():
        locked_test = (
            DomainTest.objects.select_for_update()
            .select_related("routing_transition", "domain")
            .get(id=test.id)
        )
        if (
            locked_test.routing_transition_id is None
            or locked_test.status != DomainTest.Status.PENDING
            or locked_test.expires_at <= now
            or locked_test.expected_route_kind != arrival_kind
            or locked_test.received_message_id is not None
            or message.domain_id != locked_test.domain_id
        ):
            return False
        transition = InboundRoutingTransition.objects.select_for_update().get(
            id=locked_test.routing_transition_id
        )
        domain = Domain.objects.select_for_update().get(id=locked_test.domain_id)
        if (
            transition.status != InboundRoutingTransition.Status.WAITING_TEST
            or transition.generation != locked_test.setup_generation
            or transition.to_mode != locked_test.expected_setup_mode
            or domain.status == Domain.Status.DISABLED
            or not transition.routes.filter(
                is_active=True,
                setup_generation=transition.generation,
                kind=locked_test.expected_route_kind,
            ).exists()
        ):
            return False

        grace_until = now + routing_grace_period()
        target_route_ids = transition.routes.values_list("id", flat=True)
        domain.inbound_routes.filter(
            is_active=True,
            kind=route_kind_for_mode(transition.from_mode),
            setup_generation=domain.inbound_setup_generation,
        ).exclude(id__in=target_route_ids).update(
            grace_until=grace_until,
            updated_at=now,
        )
        transition.routes.update(grace_until=None, is_active=True, updated_at=now)

        locked_test.status = DomainTest.Status.RECEIVED
        locked_test.received_message = message
        locked_test.save(update_fields=("status", "received_message", "updated_at"))
        DomainTest.objects.filter(
            domain=domain,
            status=DomainTest.Status.PENDING,
        ).exclude(id=locked_test.id).update(status=DomainTest.Status.EXPIRED, updated_at=now)

        transition.status = InboundRoutingTransition.Status.GRACE
        transition.test_received_at = now
        transition.cutover_at = now
        transition.grace_until = grace_until
        transition.error_code = ""
        transition.error_message = ""
        transition.save(
            update_fields=(
                "status",
                "test_received_at",
                "cutover_at",
                "grace_until",
                "error_code",
                "error_message",
                "updated_at",
            )
        )

        domain.setup_mode = transition.to_mode
        domain.inbound_setup_generation = transition.generation
        domain.status = Domain.Status.READY
        domain.ownership_verified = True
        domain.verified_at = domain.verified_at or now
        domain.inbound_ready = True
        domain.error_code = ""
        domain.error_message = ""
        domain.last_checked_at = now
        domain.save(
            update_fields=(
                "setup_mode",
                "inbound_setup_generation",
                "status",
                "ownership_verified",
                "verified_at",
                "inbound_ready",
                "error_code",
                "error_message",
                "last_checked_at",
                "updated_at",
            )
        )
        if transition.to_mode == Domain.SetupMode.DIRECT_MX:
            domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.MX).update(
                is_required=True,
                updated_at=now,
            )
            domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION).update(
                is_required=True,
                updated_at=now,
            )
        else:
            domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.MX).delete()
            if domain.outbound_status == Domain.OutboundStatus.DISABLED:
                domain.dns_records.filter(purpose=DomainDNSRecord.Purpose.SES_VERIFICATION).delete()
        _enqueue_job(
            domain=domain,
            kind="complete_routing_transition",
            idempotency_key=f"routing-transition:complete:{transition.id}",
            payload={
                "domain_id": str(domain.id),
                "transition_id": str(transition.id),
                "generation": transition.generation,
            },
            due_at=grace_until,
        )
        AuditEvent.objects.create(
            domain=domain,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.receiving_route_cutover",
            object_type="InboundRoutingTransition",
            object_id=transition.id,
            request_id=f"routing-transition:{transition.id}:cutover",
            metadata={
                "from": transition.from_mode,
                "to": transition.to_mode,
                "generation": transition.generation,
                "grace_until": grace_until.isoformat(),
            },
        )
        return True


def complete_expired_routing_transition(
    transition: InboundRoutingTransition,
    *,
    now=None,
) -> bool:
    now = now or timezone.now()
    with transaction.atomic():
        locked = (
            InboundRoutingTransition.objects.select_for_update()
            .select_related("domain")
            .get(id=transition.id)
        )
        if locked.status == InboundRoutingTransition.Status.COMPLETE:
            return False
        if (
            locked.status != InboundRoutingTransition.Status.GRACE
            or locked.grace_until is None
            or locked.grace_until > now
        ):
            return False
        target_route_ids = locked.routes.values_list("id", flat=True)
        locked.domain.inbound_routes.filter(
            is_active=True,
            kind=route_kind_for_mode(locked.from_mode),
            grace_until=locked.grace_until,
        ).exclude(id__in=target_route_ids).update(is_active=False, updated_at=now)
        locked.status = InboundRoutingTransition.Status.COMPLETE
        locked.completed_at = now
        locked.save(update_fields=("status", "completed_at", "updated_at"))
        AuditEvent.objects.create(
            domain=locked.domain,
            actor_type=AuditEvent.ActorType.SYSTEM,
            event_type="domain.receiving_route_transition_completed",
            object_type="InboundRoutingTransition",
            object_id=locked.id,
            request_id=f"routing-transition:{locked.id}:complete",
            metadata={
                "from": locked.from_mode,
                "to": locked.to_mode,
                "generation": locked.generation,
            },
        )
        return True
