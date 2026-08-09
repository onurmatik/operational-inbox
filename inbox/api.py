from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja import NinjaAPI, Schema, Status
from ninja.errors import AuthenticationError, AuthorizationError, HttpError
from ninja.errors import ValidationError as NinjaValidationError
from ninja.security import HttpBearer, django_auth
from pydantic import Field

from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Classification,
    Conversation,
    ConversationTag,
    Domain,
    DomainTest,
    DurableJob,
    InboundRoutingTransition,
    Message,
    Notification,
    OutboundMessage,
    ReplyDraft,
    Report,
)
from inbox.services.attachments import (
    AttachmentGoneError,
    AttachmentLockedError,
    authorized_attachment_url,
)
from inbox.services.conversations import apply_conversation_action
from inbox.services.domains import DomainClaimConflict, create_domain, ensure_domain_test
from inbox.services.drafts import (
    approve_exact_revision,
    create_authored_draft,
    create_draft,
    resend_outbound,
    revise_draft,
)
from inbox.services.entitlements import for_user
from inbox.services.jobs import (
    enqueue_job,
    request_outbound_provisioning,
    retry_domain_provisioning,
)
from inbox.services.routing_transitions import (
    ACTIVE_TRANSITION_STATUSES,
    begin_routing_transition,
    cancel_routing_transition,
    ensure_routing_transition_test,
)
from inbox.services.tags import add_conversation_tag, normalize_tag, remove_conversation_tag

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        fields: dict[str, list[str]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}
        super().__init__(message)


class ErrorSchema(Schema):
    code: str
    message: str
    fields: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str


class DomainInput(Schema):
    hostname: str = Field(min_length=3, max_length=253)
    setup_mode: Literal["DIRECT_MX", "PROVIDER_FORWARD"]


class RoutingTransitionInput(Schema):
    target_mode: Literal["DIRECT_MX", "PROVIDER_FORWARD"]


class ConversationTagInput(Schema):
    tag: str = Field(min_length=1, max_length=64)


class ConversationActionInput(Schema):
    action: Literal["star", "unstar", "archive", "trash", "restore"]


class ClassificationInput(Schema):
    category: Literal["ACTIONABLE", "INFORMATIONAL", "SUSPICIOUS", "UNCERTAIN"]
    urgency: Literal["LOW", "NORMAL", "HIGH", "CRITICAL"] = "NORMAL"
    topic: str = Field(default="", max_length=120)
    summary: str = Field(default="", max_length=1200)
    recommended_action: str = Field(default="", max_length=1200)
    requires_reply: bool = False


class RevisionInput(Schema):
    subject: str = Field(min_length=1, max_length=998)
    body_text: str = Field(min_length=1, max_length=20000)


class ApprovalInput(Schema):
    revision_id: uuid.UUID
    content_hash: str = Field(min_length=64, max_length=64)


class TokenInput(Schema):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Literal["read", "write", "approve_send"]]
    all_domains: bool = False


class ScopedBearer(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> APIToken | None:
        if not token.startswith("oi_") or len(token) < 40:
            return None
        candidates = (
            APIToken.objects.filter(
                prefix=token[:10],
                revoked_at__isnull=True,
                owner__is_active=True,
                owner__email_verified_at__isnull=False,
            )
            .filter(
                Q(domain__isnull=True)
                | Q(
                    domain__status__in=[
                        Domain.Status.PROVISIONING,
                        Domain.Status.PENDING_DNS,
                        Domain.Status.PENDING_TEST,
                        Domain.Status.READY,
                        Domain.Status.ERROR,
                        Domain.Status.DEGRADED,
                    ]
                )
            )
            .select_related("domain", "owner")
        )
        for candidate in candidates:
            if candidate.is_active and candidate.matches(token):
                APIToken.objects.filter(id=candidate.id).update(last_used_at=timezone.now())
                return candidate
        return None


bearer_auth = ScopedBearer()
authenticated = [django_auth, bearer_auth]
api = NinjaAPI(
    title="Operational Inbox API",
    version="1.0.0",
    urls_namespace="api-v1",
    description="Tenant-scoped operational email API. Session calls require CSRF.",
)


def _request_id(request: HttpRequest) -> str:
    return getattr(request, "request_id", "api")


@api.exception_handler(APIError)
def handle_api_error(request: HttpRequest, exc: APIError):
    return api.create_response(
        request,
        {
            "code": exc.code,
            "message": exc.message,
            "fields": exc.fields,
            "request_id": _request_id(request),
        },
        status=exc.status,
    )


@api.exception_handler(Http404)
def handle_not_found(request: HttpRequest, exc: Http404):
    return api.create_response(
        request,
        {
            "code": "not_found",
            "message": "The requested resource was not found.",
            "fields": {},
            "request_id": _request_id(request),
        },
        status=404,
    )


@api.exception_handler(NinjaValidationError)
def handle_validation_error(request: HttpRequest, exc: NinjaValidationError):
    fields: dict[str, list[str]] = {}
    for error in exc.errors:
        location = ".".join(str(part) for part in error.get("loc", []) if part not in {"body"})
        fields.setdefault(location or "request", []).append(str(error.get("msg", "Invalid value")))
    return api.create_response(
        request,
        {
            "code": "validation_error",
            "message": "Correct the highlighted fields and try again.",
            "fields": fields,
            "request_id": _request_id(request),
        },
        status=422,
    )


@api.exception_handler(AuthenticationError)
def handle_authentication_error(request: HttpRequest, exc: AuthenticationError):
    return api.create_response(
        request,
        {
            "code": "authentication_required",
            "message": "Valid session or bearer-token authentication is required.",
            "fields": {},
            "request_id": _request_id(request),
        },
        status=401,
    )


@api.exception_handler(AuthorizationError)
def handle_authorization_error(request: HttpRequest, exc: AuthorizationError):
    return api.create_response(
        request,
        {
            "code": "authorization_failed",
            "message": "The request was not authorized.",
            "fields": {},
            "request_id": _request_id(request),
        },
        status=403,
    )


@api.exception_handler(HttpError)
def handle_http_error(request: HttpRequest, exc: HttpError):
    return api.create_response(
        request,
        {
            "code": "request_error",
            "message": str(exc.message),
            "fields": {},
            "request_id": _request_id(request),
        },
        status=exc.status_code,
    )


def require_scope(request: HttpRequest, scope: str) -> None:
    auth = request.auth
    if isinstance(auth, APIToken):
        if not for_user(auth.owner).api:
            raise APIError(
                "upgrade_required",
                "Operational Inbox Pro is required for API access.",
                status=403,
            )
        if not auth.has_scope(scope):
            raise APIError("insufficient_scope", "This token lacks the required scope.", status=403)
        return
    user = request.user
    if not user.is_authenticated or not user.is_email_verified:
        raise APIError("authentication_required", "Authentication is required.", status=401)
    if not for_user(user).api:
        raise APIError(
            "upgrade_required",
            "Operational Inbox Pro is required for API access.",
            status=403,
        )


def record_api_audit(
    request: HttpRequest,
    domain: Domain,
    event_type: str,
    instance: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    auth = request.auth
    is_token = isinstance(auth, APIToken)
    actor = auth.owner if is_token else request.user
    audit_metadata = dict(metadata or {})
    if is_token:
        audit_metadata["api_token_id"] = str(auth.id)
        audit_metadata["api_token_name"] = auth.name
    AuditEvent.objects.create(
        domain=domain,
        actor_type=AuditEvent.ActorType.AGENT if is_token else AuditEvent.ActorType.OWNER,
        actor_id=auth.id if is_token else actor.id,
        event_type=event_type,
        object_type=instance.__class__.__name__,
        object_id=instance.id,
        request_id=_request_id(request),
        metadata=audit_metadata,
    )


def api_domain(request: HttpRequest, domain_id: uuid.UUID) -> Domain:
    auth = request.auth
    if isinstance(auth, APIToken):
        if auth.domain_id is not None and auth.domain_id != domain_id:
            raise Http404
        if auth.domain_id is not None:
            return auth.domain
        try:
            return Domain.objects.exclude(status=Domain.Status.DISABLED).get(
                id=domain_id,
                owner=auth.owner,
            )
        except Domain.DoesNotExist as exc:
            raise Http404 from exc
    try:
        return Domain.objects.exclude(status=Domain.Status.DISABLED).get(
            id=domain_id, owner=request.user
        )
    except Domain.DoesNotExist as exc:
        raise Http404 from exc


def api_domains_queryset(request: HttpRequest):
    auth = request.auth
    if isinstance(auth, APIToken) and auth.domain_id is not None:
        return Domain.objects.filter(id=auth.domain_id)
    owner = auth.owner if isinstance(auth, APIToken) else request.user
    return Domain.objects.filter(owner=owner).exclude(status=Domain.Status.DISABLED)


def scoped_object(model, domain: Domain, object_id: uuid.UUID):
    queryset = model.objects if hasattr(model, "objects") else model
    model_class = model if hasattr(model, "DoesNotExist") else model.model
    try:
        return queryset.get(id=object_id, domain=domain)
    except (model_class.DoesNotExist, ValueError) as exc:
        raise Http404 from exc


def _domain_dict(domain: Domain, *, details: bool = False) -> dict[str, Any]:
    transition = (
        domain.routing_transitions.filter(status__in=ACTIVE_TRANSITION_STATUSES)
        .order_by("-generation")
        .first()
    )
    result: dict[str, Any] = {
        "id": str(domain.id),
        "hostname": domain.hostname,
        "setup_mode": domain.setup_mode,
        "status": domain.status,
        "inbound_ready": domain.inbound_ready,
        "outbound_ready": domain.outbound_ready,
        "outbound_status": domain.outbound_status,
        "pending_setup_mode": transition.to_mode if transition is not None else None,
        "routing_transition": (
            {
                "id": str(transition.id),
                "generation": transition.generation,
                "from_mode": transition.from_mode,
                "to_mode": transition.to_mode,
                "status": transition.status,
                "dns_verified_at": transition.dns_verified_at,
                "cutover_at": transition.cutover_at,
                "grace_until": transition.grace_until,
                "error": (
                    {"code": transition.error_code, "message": transition.error_message}
                    if transition.error_code
                    else None
                ),
            }
            if transition is not None
            else None
        ),
        "last_checked_at": domain.last_checked_at,
        "error": (
            {"code": domain.error_code, "message": domain.public_error_message}
            if domain.error_code
            else None
        ),
        "outbound_error": (
            {
                "code": domain.outbound_error_code,
                "message": domain.public_outbound_error_message,
            }
            if domain.outbound_error_code
            else None
        ),
    }
    if details:
        result["existing_mx"] = domain.existing_mx
        result["dns_records"] = [
            {
                "id": str(record.id),
                "purpose": record.purpose,
                "type": record.record_type,
                "name": record.name,
                "value": record.value,
                "priority": record.priority,
                "required": record.is_required,
                "status": record.status,
                "error": record.error_message or None,
            }
            for record in domain.dns_records.all()
        ]
    return result


def _message_dict(message: Message, *, include_body: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(message.id),
        "direction": message.direction,
        "subject": message.subject,
        "from_address": message.from_address,
        "received_at": message.received_at,
        "viewed_at": message.viewed_at,
        "is_suspicious": message.is_suspicious,
        "is_quarantined": message.is_quarantined,
        "security": {
            "spam": message.spam_verdict,
            "virus": message.virus_verdict,
            "dkim": message.dkim_verdict,
            "spf": message.spf_verdict,
            "dmarc": message.dmarc_verdict,
        },
    }
    if include_body:
        result["text_body"] = None if message.is_quarantined else message.text_body
        result["recipients"] = [
            {"kind": item.kind, "address": item.address, "routing": item.is_routing_recipient}
            for item in message.recipients.all()
        ]
        result["attachments"] = [
            {
                "id": str(item.id),
                "name": item.display_name,
                "content_type": item.content_type,
                "size": item.size,
                "scan_status": item.scan_status,
            }
            for item in message.attachments.all()
        ]
    return result


def _conversation_dict(conversation: Conversation, *, details: bool = False) -> dict[str, Any]:
    if conversation.trashed_at is not None:
        folder = "trash"
    elif conversation.archived_at is not None:
        folder = "archive"
    else:
        folder = "inbox"
    has_quarantined = getattr(conversation, "has_quarantined", None)
    if has_quarantined is None:
        has_quarantined = conversation.messages.filter(is_quarantined=True).exists()
    new_message_count = getattr(conversation, "new_message_count", None)
    if new_message_count is None:
        new_message_count = conversation.messages.filter(
            direction=Message.Direction.INBOUND,
            viewed_at__isnull=True,
        ).count()
    result: dict[str, Any] = {
        "id": str(conversation.id),
        "domain_id": str(conversation.domain_id),
        "subject": conversation.subject,
        "folder": folder,
        "starred": conversation.starred_at is not None,
        "tags": [{"id": str(tag.id), "name": tag.name} for tag in conversation.tags.all()],
        "new_message_count": new_message_count,
        "has_quarantined": has_quarantined,
        "last_message_at": conversation.last_message_at,
    }
    if details:
        result["messages"] = [
            _message_dict(item, include_body=True)
            for item in conversation.messages.filter(
                domain=conversation.domain,
            ).prefetch_related("recipients", "attachments")
        ]
    return result


def _message_feed_dict(message: Message) -> dict[str, Any]:
    conversation = message.conversation
    if conversation.trashed_at is not None:
        folder = "trash"
    elif conversation.archived_at is not None:
        folder = "archive"
    else:
        folder = "inbox"
    return {
        "id": str(message.id),
        "received_at": message.received_at,
        "viewed_at": message.viewed_at,
        "subject": message.subject,
        "from_address": message.from_address,
        "text_preview": None if message.is_quarantined else message.text_body[:500],
        "is_suspicious": message.is_suspicious,
        "is_quarantined": message.is_quarantined,
        "domain": {
            "id": str(message.domain_id),
            "hostname": message.domain.hostname,
        },
        "mailboxes": sorted(
            {
                recipient.address
                for recipient in message.recipients.all()
                if recipient.is_routing_recipient
            }
        ),
        "conversation": {
            "id": str(conversation.id),
            "subject": conversation.subject,
            "folder": folder,
            "starred": conversation.starred_at is not None,
            "tags": [{"id": str(tag.id), "name": tag.name} for tag in conversation.tags.all()],
        },
    }


CURSOR_MAX_AGE_SECONDS = 86400 * 30
MAX_PAGE_SIZE = 100


def _encode_cursor(*, item: Any, timestamp_field: str, collection: str) -> str:
    return signing.dumps(
        {
            "at": getattr(item, timestamp_field).isoformat(),
            "collection": collection,
            "id": str(item.id),
        },
        salt="operational-inbox-cursor-v1",
        compress=True,
    )


def _decode_cursor(value: str, *, collection: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = signing.loads(
            value,
            salt="operational-inbox-cursor-v1",
            max_age=CURSOR_MAX_AGE_SECONDS,
        )
        payload_collection = payload.get("collection")
        if payload_collection != collection and not (
            payload_collection is None and collection == "conversations"
        ):
            raise ValueError("Cursor belongs to another collection.")
        return datetime.fromisoformat(payload["at"]), uuid.UUID(payload["id"])
    except Exception as exc:
        raise APIError("invalid_cursor", "The pagination cursor is invalid.") from exc


def _paginate_queryset(
    queryset,
    *,
    cursor: str | None,
    limit: int,
    timestamp_field: str,
    collection: str,
) -> tuple[list[Any], str | None]:
    page_size = min(max(limit, 1), MAX_PAGE_SIZE)
    queryset = queryset.order_by(f"-{timestamp_field}", "-id")
    if cursor:
        at, object_id = _decode_cursor(cursor, collection=collection)
        queryset = queryset.filter(
            Q(**{f"{timestamp_field}__lt": at}) | Q(**{timestamp_field: at, "id__lt": object_id})
        )
    items = list(queryset[: page_size + 1])
    has_more = len(items) > page_size
    items = items[:page_size]
    next_cursor = (
        _encode_cursor(
            item=items[-1],
            timestamp_field=timestamp_field,
            collection=collection,
        )
        if items and has_more
        else None
    )
    return items, next_cursor


@api.get("/health", auth=None, tags=["System"])
def api_health(request: HttpRequest) -> dict[str, str]:
    return {"status": "ok"}


@api.get("/domains", auth=authenticated, tags=["Domains"])
def domains_list(request: HttpRequest):
    require_scope(request, APIToken.Scope.READ)
    domains = api_domains_queryset(request)
    return {"items": [_domain_dict(item) for item in domains]}


@api.post(
    "/domains",
    auth=authenticated,
    response={200: dict, 202: dict},
    tags=["Domains"],
)
def domains_create(request: HttpRequest, payload: DomainInput):
    require_scope(request, APIToken.Scope.WRITE)
    if isinstance(request.auth, APIToken):
        raise APIError(
            "session_required",
            "Creating domains requires an owner session.",
            status=403,
        )
    try:
        domain = create_domain(
            owner=request.user,
            hostname=payload.hostname,
            setup_mode=payload.setup_mode,
        )
    except DomainClaimConflict as exc:
        if exc.existing_domain is not None:
            if exc.existing_domain.setup_mode != payload.setup_mode:
                raise APIError(
                    "domain_claim_conflict",
                    "The domain already has an active claim with different settings.",
                    status=409,
                ) from exc
            if exc.existing_domain.status == Domain.Status.PROVISIONING:
                existing, job, started = retry_domain_provisioning(exc.existing_domain)
                if started:
                    record_api_audit(
                        request,
                        exc.existing_domain,
                        "domain.provision_job_repaired",
                        existing,
                        {"job_id": str(job.id)},
                    )
            return Status(200, _domain_dict(exc.existing_domain))
        raise APIError(
            "domain_claim_conflict",
            "The domain is not available for a new ownership claim.",
            status=409,
        ) from exc
    except DjangoValidationError as exc:
        raise APIError(
            "validation_error",
            "The domain claim could not be created.",
            fields={"hostname": exc.messages},
        ) from exc
    enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={
            "domain_id": str(domain.id),
            "setup_generation": domain.inbound_setup_generation,
            "setup_mode": domain.setup_mode,
        },
        domain=domain,
    )
    record_api_audit(
        request,
        domain,
        "domain.created",
        domain,
        {"setup_mode": domain.setup_mode},
    )
    return Status(202, _domain_dict(domain))


@api.get("/domains/{domain_id}", auth=authenticated, tags=["Domains"])
def domains_detail(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    return _domain_dict(domain, details=True)


@api.post(
    "/domains/{domain_id}/retry",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_retry_provisioning(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    try:
        domain, job, started = retry_domain_provisioning(domain)
    except DjangoValidationError as exc:
        raise APIError(
            "domain_retry_not_allowed",
            "; ".join(exc.messages),
            status=409,
        ) from exc
    if started:
        record_api_audit(
            request,
            domain,
            "domain.provision_retry_requested",
            domain,
            {"job_id": str(job.id)},
        )
    return Status(
        202,
        {
            "status": domain.status,
            "job_id": str(job.id),
            "started": started,
        },
    )


@api.post(
    "/domains/{domain_id}/routing/transition",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_start_routing_transition(
    request: HttpRequest,
    domain_id: uuid.UUID,
    payload: RoutingTransitionInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    try:
        transition, started = begin_routing_transition(domain, payload.target_mode)
    except DjangoValidationError as exc:
        raise APIError(
            "routing_transition_not_allowed",
            "; ".join(exc.messages),
            status=409,
        ) from exc
    job = enqueue_job(
        kind="provision_routing_transition",
        idempotency_key=(f"provision-routing-transition:{transition.id}:{transition.generation}"),
        payload={
            "transition_id": str(transition.id),
            "generation": transition.generation,
        },
        domain=domain,
    )
    if started:
        record_api_audit(
            request,
            domain,
            "domain.routing_transition_started",
            transition,
            {
                "from": transition.from_mode,
                "to": transition.to_mode,
                "generation": transition.generation,
                "job_id": str(job.id),
            },
        )
    return Status(
        202,
        {
            "id": str(transition.id),
            "status": transition.status,
            "from_mode": transition.from_mode,
            "to_mode": transition.to_mode,
            "generation": transition.generation,
            "job_id": str(job.id),
            "started": started,
        },
    )


@api.post(
    "/domains/{domain_id}/routing/transition/cancel",
    auth=authenticated,
    response={200: dict},
    tags=["Domains"],
)
def domains_cancel_routing_transition(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    transition = (
        domain.routing_transitions.filter(status__in=ACTIVE_TRANSITION_STATUSES)
        .order_by("-generation")
        .first()
    )
    if transition is None:
        raise APIError(
            "routing_transition_not_found",
            "There is no active receiving-route transition.",
            status=409,
        )
    try:
        cancelled = cancel_routing_transition(transition)
    except DjangoValidationError as exc:
        raise APIError(
            "routing_transition_not_cancellable",
            "; ".join(exc.messages),
            status=409,
        ) from exc
    if cancelled:
        record_api_audit(
            request,
            domain,
            "domain.routing_transition_cancelled",
            transition,
            {"generation": transition.generation},
        )
    return {"id": str(transition.id), "status": "CANCELLED", "cancelled": cancelled}


@api.post(
    "/domains/{domain_id}/outbound/enable",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_enable_outbound(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    try:
        domain, job, started = request_outbound_provisioning(domain)
    except DjangoValidationError as exc:
        raise APIError(
            "outbound_enable_not_allowed",
            "; ".join(exc.messages),
            status=409,
        ) from exc
    if started:
        record_api_audit(
            request,
            domain,
            "domain.outbound_provision_requested",
            domain,
            {"job_id": str(job.id)},
        )
    return Status(
        202,
        {
            "outbound_status": domain.outbound_status,
            "job_id": str(job.id),
            "started": started,
        },
    )


@api.post(
    "/domains/{domain_id}/check",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_check(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    transition_needs_check = domain.routing_transitions.filter(
        status__in=(
            InboundRoutingTransition.Status.WAITING_DNS,
            InboundRoutingTransition.Status.WAITING_TEST,
        )
    ).exists()
    if (
        domain.status
        not in {
            Domain.Status.PENDING_DNS,
            Domain.Status.PENDING_TEST,
            Domain.Status.READY,
            Domain.Status.DEGRADED,
        }
        and not transition_needs_check
    ) or not domain.dns_records.exists():
        raise APIError(
            "dns_instructions_not_ready",
            "DNS instructions must be ready before requesting a check.",
            status=409,
        )
    requested_at = timezone.now()
    job = enqueue_job(
        kind="dns_check",
        idempotency_key=f"dns-check:{domain.id}:{requested_at:%Y%m%d%H%M}",
        payload={"domain_id": str(domain.id)},
        domain=domain,
    )
    if job.status == DurableJob.Status.RETRY:
        expedited = DurableJob.objects.filter(
            id=job.id,
            status=DurableJob.Status.RETRY,
        ).update(due_at=requested_at, updated_at=requested_at)
        if expedited:
            job.due_at = requested_at
        else:
            job.refresh_from_db()
    if job.status in {DurableJob.Status.COMPLETE, DurableJob.Status.FAILED}:
        job = enqueue_job(
            kind="dns_check",
            idempotency_key=(f"dns-check:{domain.id}:{requested_at:%Y%m%d%H%M%S%f}"),
            payload={"domain_id": str(domain.id)},
            domain=domain,
        )
    record_api_audit(request, domain, "domain.check_requested", domain)
    return Status(202, {"status": "queued", "job_id": str(job.id)})


@api.post(
    "/domains/{domain_id}/test",
    auth=authenticated,
    response={200: dict, 201: dict},
    tags=["Domains"],
)
def domains_test(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    transition = (
        domain.routing_transitions.filter(status__in=ACTIVE_TRANSITION_STATUSES)
        .order_by("-generation")
        .first()
    )
    try:
        if transition is not None:
            if transition.status != InboundRoutingTransition.Status.WAITING_TEST:
                raise DjangoValidationError(
                    "Verify the target receiving route before preparing its test address."
                )
            test, address, created = ensure_routing_transition_test(transition)
        else:
            test, address, created = ensure_domain_test(domain)
    except DjangoValidationError as exc:
        raise APIError(
            "domain_not_ready_for_test",
            "; ".join(exc.messages),
            status=409,
        ) from exc
    except Exception as exc:
        logger.exception("Domain test route activation failed", extra={"domain_id": domain.id})
        raise APIError(
            "receiving_route_not_ready",
            "The receiving route is still being activated. Try again shortly.",
            status=503,
        ) from exc
    if created:
        record_api_audit(
            request,
            domain,
            (
                "domain.routing_transition_test_created"
                if test.routing_transition_id is not None
                else "domain.test_created"
            ),
            test,
        )
    return Status(
        201 if created else 200,
        {
            "id": str(test.id),
            "address": address,
            "expires_at": test.expires_at,
            "status": test.status,
        },
    )


@api.post(
    "/domains/{domain_id}/disable",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_disable(request: HttpRequest, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    domain.status = Domain.Status.DISABLED
    domain.inbound_ready = False
    domain.outbound_ready = False
    domain.outbound_status = Domain.OutboundStatus.DISABLED
    domain.outbound_error_code = ""
    domain.outbound_error_message = ""
    domain.save(
        update_fields=(
            "status",
            "inbound_ready",
            "outbound_ready",
            "outbound_status",
            "outbound_error_code",
            "outbound_error_message",
            "updated_at",
        )
    )
    domain.inbound_routes.update(is_active=False)
    now = timezone.now()
    domain.routing_transitions.filter(status__in=ACTIVE_TRANSITION_STATUSES).update(
        status=InboundRoutingTransition.Status.CANCELLED,
        cancelled_at=now,
        updated_at=now,
    )
    domain.tests.filter(
        status=DomainTest.Status.PENDING,
    ).update(status=DomainTest.Status.EXPIRED, updated_at=now)
    job = enqueue_job(
        kind="reconcile_receipt_rule",
        idempotency_key=f"receipt-rule:disable:{domain.id}",
        payload={},
        domain=domain,
    )
    record_api_audit(request, domain, "domain.disabled", domain)
    return Status(202, {"status": domain.status, "job_id": str(job.id)})


@api.get("/domains/{domain_id}/conversations", auth=authenticated, tags=["Conversations"])
def conversations_list(
    request: HttpRequest,
    domain_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
    folder: Literal["inbox", "starred", "archive", "trash"] = "inbox",
    mailbox: str | None = None,
    tag: str | None = None,
    new_only: bool = False,
    security: Literal["suspicious", "quarantined"] | None = None,
    q: str | None = None,
):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    queryset = Conversation.objects.filter(domain=domain).annotate(
        new_message_count=Count(
            "messages",
            filter=Q(
                messages__direction=Message.Direction.INBOUND,
                messages__viewed_at__isnull=True,
            ),
            distinct=True,
        ),
        has_quarantined=Exists(
            Message.objects.filter(conversation=OuterRef("pk"), is_quarantined=True)
        ),
    )
    if folder == "inbox":
        queryset = queryset.filter(archived_at__isnull=True, trashed_at__isnull=True)
    elif folder == "starred":
        queryset = queryset.filter(starred_at__isnull=False, trashed_at__isnull=True)
    elif folder == "archive":
        queryset = queryset.filter(archived_at__isnull=False, trashed_at__isnull=True)
    else:
        queryset = queryset.filter(trashed_at__isnull=False)
    if mailbox:
        queryset = queryset.filter(
            messages__direction=Message.Direction.INBOUND,
            messages__recipients__is_routing_recipient=True,
            messages__recipients__address__iexact=mailbox.strip(),
        ).distinct()
    if tag:
        try:
            _, normalized_tag = normalize_tag(tag)
        except DjangoValidationError as exc:
            raise APIError("invalid_tag", exc.messages[0]) from exc
        queryset = queryset.filter(tags__normalized_name=normalized_tag).distinct()
    if new_only:
        queryset = queryset.filter(new_message_count__gt=0)
    if security == "suspicious":
        queryset = queryset.filter(messages__is_suspicious=True).distinct()
    elif security == "quarantined":
        queryset = queryset.filter(messages__is_quarantined=True).distinct()
    if q:
        queryset = queryset.filter(
            Q(subject__icontains=q)
            | Q(messages__from_address__icontains=q)
            | Q(messages__text_body__icontains=q)
        ).distinct()
    items, next_cursor = _paginate_queryset(
        queryset.prefetch_related("tags"),
        cursor=cursor,
        limit=limit,
        timestamp_field="last_message_at",
        collection="conversations",
    )
    return {
        "items": [_conversation_dict(item) for item in items],
        "next_cursor": next_cursor,
    }


@api.get("/feed/messages", auth=authenticated, tags=["Message feed"])
def messages_feed(
    request: HttpRequest,
    cursor: str | None = None,
    after: str | None = None,
    limit: int = 50,
    domain_id: uuid.UUID | None = None,
    mailbox: str | None = None,
    tag: str | None = None,
    folder: Literal["inbox", "starred", "archive", "trash"] | None = None,
    new_only: bool = False,
    security: Literal["suspicious", "quarantined"] | None = None,
):
    require_scope(request, APIToken.Scope.READ)
    if cursor and after:
        raise APIError(
            "invalid_cursor",
            "Use cursor for older history or after for new mail, not both.",
        )
    domains = api_domains_queryset(request)
    if domain_id is not None:
        domain = api_domain(request, domain_id)
        domains = domains.filter(id=domain.id)
    queryset = Message.objects.filter(
        domain__in=domains,
        direction=Message.Direction.INBOUND,
    )
    if mailbox:
        queryset = queryset.filter(
            recipients__is_routing_recipient=True,
            recipients__address__iexact=mailbox.strip(),
        ).distinct()
    if tag:
        try:
            _, normalized_tag = normalize_tag(tag)
        except DjangoValidationError as exc:
            raise APIError("invalid_tag", exc.messages[0]) from exc
        queryset = queryset.filter(conversation__tags__normalized_name=normalized_tag).distinct()
    if folder == "inbox":
        queryset = queryset.filter(
            conversation__archived_at__isnull=True,
            conversation__trashed_at__isnull=True,
        )
    elif folder == "starred":
        queryset = queryset.filter(
            conversation__starred_at__isnull=False,
            conversation__trashed_at__isnull=True,
        )
    elif folder == "archive":
        queryset = queryset.filter(
            conversation__archived_at__isnull=False,
            conversation__trashed_at__isnull=True,
        )
    elif folder == "trash":
        queryset = queryset.filter(conversation__trashed_at__isnull=False)
    if new_only:
        queryset = queryset.filter(viewed_at__isnull=True)
    if security == "suspicious":
        queryset = queryset.filter(is_suspicious=True)
    elif security == "quarantined":
        queryset = queryset.filter(is_quarantined=True)
    queryset = queryset.select_related("domain", "conversation").prefetch_related(
        "recipients",
        "conversation__tags",
    )

    page_size = min(max(limit, 1), MAX_PAGE_SIZE)
    if after:
        at, object_id = _decode_cursor(after, collection="message-feed-checkpoint")
        candidates = list(
            queryset.filter(Q(received_at__gt=at) | Q(received_at=at, id__gt=object_id)).order_by(
                "received_at", "id"
            )[: page_size + 1]
        )
        has_more = len(candidates) > page_size
        items = candidates[:page_size]
        checkpoint = (
            _encode_cursor(
                item=items[-1],
                timestamp_field="received_at",
                collection="message-feed-checkpoint",
            )
            if items
            else after
        )
        next_cursor = None
    else:
        items, next_cursor = _paginate_queryset(
            queryset,
            cursor=cursor,
            limit=page_size,
            timestamp_field="received_at",
            collection="message-feed-history",
        )
        has_more = next_cursor is not None
        checkpoint = (
            _encode_cursor(
                item=items[0],
                timestamp_field="received_at",
                collection="message-feed-checkpoint",
            )
            if items and cursor is None
            else None
        )
    return {
        "items": [_message_feed_dict(item) for item in items],
        "next_cursor": next_cursor,
        "checkpoint": checkpoint,
        "has_more": has_more,
    }


@api.get(
    "/domains/{domain_id}/conversations/{conversation_id}",
    auth=authenticated,
    tags=["Conversations"],
)
def conversations_detail(request: HttpRequest, domain_id: uuid.UUID, conversation_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    conversation = scoped_object(
        Conversation.objects.prefetch_related("tags"),
        domain,
        conversation_id,
    )
    return _conversation_dict(conversation, details=True)


@api.post(
    "/domains/{domain_id}/conversations/{conversation_id}/action",
    auth=authenticated,
    tags=["Conversations"],
)
def conversations_action(
    request: HttpRequest,
    domain_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationActionInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    event_types = {
        "star": "conversation.starred",
        "unstar": "conversation.unstarred",
        "archive": "conversation.archived",
        "trash": "conversation.trashed",
        "restore": "conversation.restored",
    }
    with transaction.atomic():
        conversation = scoped_object(
            Conversation.objects.select_for_update().prefetch_related("tags"),
            domain,
            conversation_id,
        )
        result = apply_conversation_action(conversation, payload.action)
        if result.state_changed:
            record_api_audit(
                request,
                domain,
                event_types[payload.action],
                conversation,
                {"action": payload.action},
            )
    return {"changed": result.state_changed, **_conversation_dict(conversation)}


@api.post(
    "/domains/{domain_id}/conversations/{conversation_id}/tags",
    auth=authenticated,
    response={200: dict, 201: dict},
    tags=["Conversation tags"],
)
def conversations_tags_add(
    request: HttpRequest,
    domain_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationTagInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    conversation = scoped_object(Conversation, domain, conversation_id)
    try:
        tag, created = add_conversation_tag(conversation, payload.tag)
    except DjangoValidationError as exc:
        raise APIError("invalid_tag", exc.messages[0]) from exc
    if created:
        record_api_audit(
            request,
            domain,
            "conversation.tag_added",
            tag,
            {"conversation_id": str(conversation.id), "tag": tag.normalized_name},
        )
    return Status(
        201 if created else 200,
        {"id": str(tag.id), "name": tag.name, "created": created},
    )


@api.delete(
    "/domains/{domain_id}/conversations/{conversation_id}/tags/{tag_id}",
    auth=authenticated,
    tags=["Conversation tags"],
)
def conversations_tags_remove(
    request: HttpRequest,
    domain_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tag_id: uuid.UUID,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    conversation = scoped_object(Conversation, domain, conversation_id)
    tag = scoped_object(ConversationTag, domain, tag_id)
    if tag.conversation_id != conversation.id:
        raise Http404
    tag_name = tag.normalized_name
    remove_conversation_tag(conversation, tag_name)
    record_api_audit(
        request,
        domain,
        "conversation.tag_removed",
        tag,
        {"conversation_id": str(conversation.id), "tag": tag_name},
    )
    return {"removed": True, "tag": tag_name}


@api.post(
    "/domains/{domain_id}/messages/{message_id}/classification",
    auth=authenticated,
    response={201: dict},
    tags=["Classifications"],
)
def classifications_override(
    request: HttpRequest,
    domain_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: ClassificationInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    message = scoped_object(Message, domain, message_id)
    with transaction.atomic():
        previous = (
            Classification.objects.select_for_update()
            .filter(message=message, is_current=True)
            .first()
        )
        if previous:
            previous.is_current = False
            previous.save(update_fields=("is_current", "updated_at"))
        classification = Classification.objects.create(
            domain=domain,
            message=message,
            source=Classification.Source.OWNER,
            category=payload.category,
            urgency=payload.urgency,
            topic=payload.topic,
            summary=payload.summary,
            recommended_action=payload.recommended_action,
            requires_reply=payload.requires_reply,
            supersedes=previous,
        )
    record_api_audit(
        request,
        domain,
        "classification.overridden",
        classification,
        {"category": classification.category, "urgency": classification.urgency},
    )
    return Status(201, {"id": str(classification.id), "category": classification.category})


@api.post(
    "/domains/{domain_id}/conversations/{conversation_id}/drafts",
    auth=authenticated,
    response={201: dict},
    tags=["Drafts"],
)
def drafts_generate(request: HttpRequest, domain_id: uuid.UUID, conversation_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    conversation = scoped_object(Conversation, domain, conversation_id)
    message = conversation.messages.filter(direction=Message.Direction.INBOUND).last()
    if message is None:
        raise APIError("no_inbound_message", "No inbound message is available for drafting.")
    try:
        draft = create_draft(message)
    except Exception as exc:
        raise APIError(
            "draft_unavailable",
            "A draft could not be generated. The message remains unchanged.",
            status=503,
        ) from exc
    record_api_audit(request, domain, "draft.generated", draft)
    return Status(
        201,
        {
            "id": str(draft.id),
            "revision_id": str(draft.current_revision_id),
            "content_hash": draft.current_revision.content_hash,
        },
    )


@api.post(
    "/domains/{domain_id}/conversations/{conversation_id}/drafts/authored",
    auth=authenticated,
    response={201: dict},
    tags=["Drafts"],
)
def drafts_create_authored(
    request: HttpRequest,
    domain_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: RevisionInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    conversation = scoped_object(Conversation, domain, conversation_id)
    message = conversation.messages.filter(direction=Message.Direction.INBOUND).last()
    if message is None:
        raise APIError("no_inbound_message", "No inbound message is available for drafting.")
    owner = domain.owner if isinstance(request.auth, APIToken) else request.user
    try:
        draft = create_authored_draft(
            message=message,
            owner=owner,
            subject=payload.subject,
            body_text=payload.body_text,
        )
    except DjangoValidationError as exc:
        raise APIError("draft_unavailable", "; ".join(exc.messages), status=409) from exc
    record_api_audit(request, domain, "draft.created", draft, {"source": "agent"})
    revision = draft.current_revision
    return Status(
        201,
        {
            "id": str(draft.id),
            "revision_id": str(revision.id),
            "content_hash": revision.content_hash,
            "subject": revision.subject,
            "body_text": revision.body_text,
        },
    )


@api.get(
    "/domains/{domain_id}/drafts/{draft_id}",
    auth=authenticated,
    tags=["Drafts"],
)
def drafts_detail(request: HttpRequest, domain_id: uuid.UUID, draft_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    draft = scoped_object(ReplyDraft, domain, draft_id)
    revision = draft.current_revision
    if revision is None:
        raise Http404
    return {
        "id": str(draft.id),
        "conversation_id": str(draft.conversation_id),
        "is_stale": draft.is_stale,
        "current_revision": {
            "id": str(revision.id),
            "number": revision.number,
            "subject": revision.subject,
            "body_text": revision.body_text,
            "content_hash": revision.content_hash,
        },
    }


@api.post(
    "/domains/{domain_id}/drafts/{draft_id}/revisions",
    auth=authenticated,
    response={201: dict},
    tags=["Drafts"],
)
def drafts_revise(
    request: HttpRequest,
    domain_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: RevisionInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    draft = scoped_object(ReplyDraft, domain, draft_id)
    owner = domain.owner if isinstance(request.auth, APIToken) else request.user
    try:
        revision = revise_draft(
            draft=draft, owner=owner, subject=payload.subject, body_text=payload.body_text
        )
    except DjangoValidationError as exc:
        raise APIError("stale_draft", "; ".join(exc.messages), status=409) from exc
    record_api_audit(request, domain, "draft.revised", revision)
    return Status(
        201,
        {
            "id": str(revision.id),
            "number": revision.number,
            "content_hash": revision.content_hash,
        },
    )


@api.post(
    "/domains/{domain_id}/drafts/{draft_id}/approval",
    auth=authenticated,
    response={202: dict},
    tags=["Drafts"],
)
def drafts_approve(
    request: HttpRequest,
    domain_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: ApprovalInput,
):
    require_scope(request, APIToken.Scope.APPROVE_SEND)
    domain = api_domain(request, domain_id)
    draft = scoped_object(ReplyDraft, domain, draft_id)
    owner = domain.owner if isinstance(request.auth, APIToken) else request.user
    try:
        outbound = approve_exact_revision(
            draft=draft,
            revision_id=payload.revision_id,
            content_hash=payload.content_hash,
            owner=owner,
        )
    except DjangoValidationError as exc:
        raise APIError("stale_revision", "; ".join(exc.messages), status=409) from exc
    enqueue_job(
        kind="send_outbound",
        idempotency_key=f"outbound:{outbound.id}",
        payload={"outbound_id": str(outbound.id)},
        domain=domain,
    )
    record_api_audit(
        request,
        domain,
        "draft.approved_and_queued",
        outbound,
        {"revision_id": str(payload.revision_id)},
    )
    return Status(202, {"outbound_id": str(outbound.id), "status": outbound.status})


@api.get(
    "/domains/{domain_id}/outbound/{outbound_id}",
    auth=authenticated,
    tags=["Outbound"],
)
def outbound_status(request: HttpRequest, domain_id: uuid.UUID, outbound_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    outbound = scoped_object(OutboundMessage, domain, outbound_id)
    return {
        "id": str(outbound.id),
        "status": outbound.status,
        "attempt": outbound.attempt_number,
        "accepted_at": outbound.accepted_at,
        "delivered_at": outbound.delivered_at,
        "error": (
            {"code": outbound.error_code, "message": outbound.public_error_message}
            if outbound.error_code
            else None
        ),
    }


@api.post(
    "/domains/{domain_id}/outbound/{outbound_id}/resend",
    auth=authenticated,
    response={202: dict},
    tags=["Outbound"],
)
def outbound_resend(request: HttpRequest, domain_id: uuid.UUID, outbound_id: uuid.UUID):
    require_scope(request, APIToken.Scope.APPROVE_SEND)
    domain = api_domain(request, domain_id)
    original = scoped_object(OutboundMessage, domain, outbound_id)
    owner = domain.owner if isinstance(request.auth, APIToken) else request.user
    try:
        resend = resend_outbound(original, owner=owner)
    except DjangoValidationError as exc:
        raise APIError("resend_not_allowed", "; ".join(exc.messages), status=409) from exc
    enqueue_job(
        kind="send_outbound",
        idempotency_key=f"outbound:{resend.id}",
        payload={"outbound_id": str(resend.id)},
        domain=domain,
    )
    record_api_audit(
        request,
        domain,
        "outbound.resend_queued",
        resend,
        {"original_id": str(original.id)},
    )
    return Status(202, {"outbound_id": str(resend.id), "status": resend.status})


@api.get("/domains/{domain_id}/reports", auth=authenticated, tags=["Reports"])
def reports_list(
    request: HttpRequest,
    domain_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    reports, next_cursor = _paginate_queryset(
        Report.objects.filter(domain=domain),
        cursor=cursor,
        limit=limit,
        timestamp_field="created_at",
        collection="reports",
    )
    return {
        "items": [
            {
                "id": str(report.id),
                "kind": report.kind,
                "status": report.status,
                "generation_mode": report.generation_mode,
                "title": report.title,
                "content": report.content,
                "period_start": report.period_start,
                "period_end": report.period_end,
                "created_at": report.created_at,
            }
            for report in reports
        ],
        "next_cursor": next_cursor,
    }


@api.get("/domains/{domain_id}/notifications", auth=authenticated, tags=["Notifications"])
def notifications_list(
    request: HttpRequest,
    domain_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    notifications, next_cursor = _paginate_queryset(
        Notification.objects.filter(domain=domain, channel=Notification.Channel.IN_APP),
        cursor=cursor,
        limit=limit,
        timestamp_field="created_at",
        collection="notifications",
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "kind": item.kind,
                "status": item.status,
                "title": item.title,
                "body": item.body,
                "created_at": item.created_at,
            }
            for item in notifications
        ],
        "next_cursor": next_cursor,
    }


@api.post(
    "/domains/{domain_id}/notifications/{notification_id}/read",
    auth=authenticated,
    tags=["Notifications"],
)
def notifications_read(request: HttpRequest, domain_id: uuid.UUID, notification_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    notification = scoped_object(Notification, domain, notification_id)
    notification.status = Notification.Status.READ
    notification.read_at = timezone.now()
    notification.save(update_fields=("status", "read_at", "updated_at"))
    record_api_audit(request, domain, "notification.read", notification)
    return {"status": "read"}


@api.get("/domains/{domain_id}/audit", auth=authenticated, tags=["Audit"])
def audit_list(
    request: HttpRequest,
    domain_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    events, next_cursor = _paginate_queryset(
        AuditEvent.objects.filter(domain=domain),
        cursor=cursor,
        limit=limit,
        timestamp_field="created_at",
        collection="audit",
    )
    return {
        "items": [
            {
                "id": str(event.id),
                "actor_type": event.actor_type,
                "event_type": event.event_type,
                "object_type": event.object_type,
                "object_id": str(event.object_id) if event.object_id else None,
                "request_id": event.request_id,
                "metadata": event.metadata,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "next_cursor": next_cursor,
    }


@api.get(
    "/domains/{domain_id}/attachments/{attachment_id}/url",
    auth=authenticated,
    tags=["Attachments"],
)
def attachments_url(request: HttpRequest, domain_id: uuid.UUID, attachment_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    domain = api_domain(request, domain_id)
    attachment = scoped_object(Attachment, domain, attachment_id)
    try:
        authorized = authorized_attachment_url(attachment=attachment, domain=domain)
    except AttachmentGoneError as exc:
        raise APIError("attachment_expired", str(exc), status=410) from exc
    except AttachmentLockedError as exc:
        raise APIError("attachment_locked", str(exc), status=423) from exc
    record_api_audit(request, domain, "attachment.url_issued", attachment)
    return {"url": authorized.url, "expires_in": authorized.expires_in}


@api.post(
    "/domains/{domain_id}/tokens",
    auth=authenticated,
    response={201: dict},
    tags=["Tokens"],
)
def tokens_create(request: HttpRequest, domain_id: uuid.UUID, payload: TokenInput):
    require_scope(request, APIToken.Scope.WRITE)
    domain = api_domain(request, domain_id)
    if isinstance(request.auth, APIToken):
        raise APIError(
            "session_required", "API tokens must be created from an owner session.", status=403
        )
    try:
        token, raw = APIToken.issue(
            domain=None if payload.all_domains else domain,
            owner=request.user,
            name=payload.name,
            scopes=list(payload.scopes),
        )
    except DjangoValidationError as exc:
        raise APIError("validation_error", "; ".join(exc.messages)) from exc
    record_api_audit(
        request,
        domain,
        "api_token.created",
        token,
        {"scopes": token.scopes, "all_domains": token.domain_id is None},
    )
    return Status(
        201,
        {
            "id": str(token.id),
            "token": raw,
            "prefix": token.prefix,
            "scopes": token.scopes,
            "all_domains": token.domain_id is None,
            "shown_once": True,
        },
    )
