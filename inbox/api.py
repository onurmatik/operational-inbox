from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Literal

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest
from django.utils import timezone
from django.utils.text import slugify
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
    Domain,
    DurableJob,
    Message,
    Notification,
    Organization,
    OutboundMessage,
    Project,
    ReplyDraft,
    Report,
    ReportSchedule,
    RetentionPolicy,
)
from inbox.services.attachments import (
    AttachmentGoneError,
    AttachmentLockedError,
    authorized_attachment_url,
)
from inbox.services.domains import create_domain, create_domain_test
from inbox.services.drafts import (
    approve_exact_revision,
    create_draft,
    resend_outbound,
    revise_draft,
)
from inbox.services.jobs import enqueue_job

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


class OrganizationInput(Schema):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", max_length=64)
    project_name: str = Field(default="Default project", min_length=1, max_length=120)


class ProjectInput(Schema):
    name: str = Field(min_length=1, max_length=120)


class DomainInput(Schema):
    project_id: uuid.UUID
    hostname: str = Field(min_length=3, max_length=253)
    setup_mode: Literal["DIRECT_MX", "PROVIDER_FORWARD"]


class ConversationStatusInput(Schema):
    status: Literal["OPEN", "WAITING_EXTERNAL", "RESOLVED"]


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


class ScopedBearer(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> APIToken | None:
        if not token.startswith("oi_") or len(token) < 40:
            return None
        candidates = APIToken.objects.filter(
            prefix=token[:10],
            revoked_at__isnull=True,
            organization__is_active=True,
            owner__is_active=True,
            owner__email_verified_at__isnull=False,
        ).select_related("organization", "owner")
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
        if not auth.has_scope(scope):
            raise APIError("insufficient_scope", "This token lacks the required scope.", status=403)
        return
    user = request.user
    if not user.is_authenticated or not user.is_email_verified:
        raise APIError("authentication_required", "Authentication is required.", status=401)


def record_api_audit(
    request: HttpRequest,
    organization: Organization,
    event_type: str,
    instance: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    auth = request.auth
    actor = auth.owner if isinstance(auth, APIToken) else request.user
    AuditEvent.objects.create(
        organization=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=actor.id,
        event_type=event_type,
        object_type=instance.__class__.__name__,
        object_id=instance.id,
        request_id=_request_id(request),
        metadata=metadata or {},
    )


def api_organization(request: HttpRequest, organization_id: uuid.UUID) -> Organization:
    auth = request.auth
    if isinstance(auth, APIToken):
        if auth.organization_id != organization_id:
            raise Http404
        return auth.organization
    try:
        return Organization.objects.get(id=organization_id, owner=request.user, is_active=True)
    except Organization.DoesNotExist as exc:
        raise Http404 from exc


def scoped_object(model, organization: Organization, object_id: uuid.UUID):
    try:
        return model.objects.get(id=object_id, organization=organization)
    except (model.DoesNotExist, ValueError) as exc:
        raise Http404 from exc


def _org_dict(organization: Organization) -> dict[str, Any]:
    return {
        "id": str(organization.id),
        "name": organization.name,
        "slug": organization.slug,
        "timezone": organization.timezone,
    }


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "organization_id": str(project.organization_id),
        "name": project.name,
        "slug": project.slug,
        "active": project.is_active,
    }


def _domain_dict(domain: Domain, *, details: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(domain.id),
        "project_id": str(domain.project_id),
        "hostname": domain.hostname,
        "setup_mode": domain.setup_mode,
        "status": domain.status,
        "inbound_ready": domain.inbound_ready,
        "outbound_ready": domain.outbound_ready,
        "last_checked_at": domain.last_checked_at,
        "error": (
            {"code": domain.error_code, "message": domain.error_message}
            if domain.error_code
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
    result: dict[str, Any] = {
        "id": str(conversation.id),
        "project_id": str(conversation.project_id),
        "subject": conversation.subject,
        "status": conversation.status,
        "last_message_at": conversation.last_message_at,
    }
    if details:
        result["messages"] = [
            _message_dict(item, include_body=True)
            for item in conversation.messages.filter(
                organization=conversation.organization,
                project=conversation.project,
            ).prefetch_related("recipients", "attachments")
        ]
    return result


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


@api.get("/organizations", auth=authenticated, tags=["Organizations"])
def organizations_list(request: HttpRequest):
    require_scope(request, APIToken.Scope.READ)
    if isinstance(request.auth, APIToken):
        organizations = [request.auth.organization]
    else:
        organizations = Organization.objects.filter(owner=request.user, is_active=True)
    return {"items": [_org_dict(item) for item in organizations]}


@api.post("/organizations", auth=authenticated, response={201: dict}, tags=["Organizations"])
def organizations_create(request: HttpRequest, payload: OrganizationInput):
    require_scope(request, APIToken.Scope.WRITE)
    if isinstance(request.auth, APIToken):
        raise APIError(
            "session_required", "Creating organizations requires an owner session.", status=403
        )
    with transaction.atomic():
        base = slugify(payload.name)[:70] or "organization"
        slug = base
        index = 2
        while Organization.objects.filter(owner=request.user, slug=slug).exists():
            slug = f"{base[:65]}-{index}"
            index += 1
        organization = Organization.objects.create(
            owner=request.user, name=payload.name, slug=slug, timezone=payload.timezone
        )
        Project.objects.create(
            organization=organization,
            name=payload.project_name,
            slug=slugify(payload.project_name)[:70] or "default",
        )
        ReportSchedule.objects.create(organization=organization)
        RetentionPolicy.objects.create(organization=organization)
    record_api_audit(request, organization, "organization.created", organization)
    return Status(201, _org_dict(organization))


@api.get("/organizations/{organization_id}/projects", auth=authenticated, tags=["Projects"])
def projects_list(request: HttpRequest, organization_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    return {
        "items": [
            _project_dict(item)
            for item in Project.objects.filter(organization=organization).order_by("name")
        ]
    }


@api.post(
    "/organizations/{organization_id}/projects",
    auth=authenticated,
    response={201: dict},
    tags=["Projects"],
)
def projects_create(request: HttpRequest, organization_id: uuid.UUID, payload: ProjectInput):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    if (
        Project.objects.filter(organization=organization, is_active=True).count()
        >= settings.MAX_PROJECTS_PER_ORGANIZATION
    ):
        raise APIError(
            "project_limit_reached",
            "The organization has reached its project limit.",
            status=409,
        )
    base = slugify(payload.name)[:70] or "project"
    slug = base
    index = 2
    while Project.objects.filter(organization=organization, slug=slug).exists():
        slug = f"{base[:65]}-{index}"
        index += 1
    project = Project.objects.create(organization=organization, name=payload.name, slug=slug)
    record_api_audit(request, organization, "project.created", project)
    return Status(201, _project_dict(project))


@api.get("/organizations/{organization_id}/domains", auth=authenticated, tags=["Domains"])
def domains_list(request: HttpRequest, organization_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    return {
        "items": [
            _domain_dict(item)
            for item in Domain.objects.filter(organization=organization).order_by("hostname")
        ]
    }


@api.post(
    "/organizations/{organization_id}/domains",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_create(request: HttpRequest, organization_id: uuid.UUID, payload: DomainInput):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    try:
        project = Project.objects.get(
            id=payload.project_id, organization=organization, is_active=True
        )
    except Project.DoesNotExist as exc:
        raise Http404 from exc
    try:
        domain = create_domain(
            organization=organization,
            project=project,
            hostname=payload.hostname,
            setup_mode=payload.setup_mode,
        )
    except DjangoValidationError as exc:
        raise APIError(
            "validation_error",
            "The domain claim could not be created.",
            fields={"hostname": exc.messages},
        ) from exc
    enqueue_job(
        kind="provision_domain",
        idempotency_key=f"provision-domain:{domain.id}",
        payload={"domain_id": str(domain.id)},
        organization=organization,
    )
    record_api_audit(
        request,
        organization,
        "domain.created",
        domain,
        {"setup_mode": domain.setup_mode},
    )
    return Status(202, _domain_dict(domain))


@api.get(
    "/organizations/{organization_id}/domains/{domain_id}", auth=authenticated, tags=["Domains"]
)
def domains_detail(request: HttpRequest, organization_id: uuid.UUID, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    domain = scoped_object(Domain, organization, domain_id)
    return _domain_dict(domain, details=True)


@api.post(
    "/organizations/{organization_id}/domains/{domain_id}/check",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_check(request: HttpRequest, organization_id: uuid.UUID, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    domain = scoped_object(Domain, organization, domain_id)
    if domain.status not in {
        Domain.Status.PENDING_DNS,
        Domain.Status.PENDING_TEST,
        Domain.Status.READY,
        Domain.Status.DEGRADED,
    } or not domain.dns_records.exists():
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
        organization=organization,
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
            idempotency_key=(
                f"dns-check:{domain.id}:{requested_at:%Y%m%d%H%M%S%f}"
            ),
            payload={"domain_id": str(domain.id)},
            organization=organization,
        )
    record_api_audit(request, organization, "domain.check_requested", domain)
    return Status(202, {"status": "queued", "job_id": str(job.id)})


@api.post(
    "/organizations/{organization_id}/domains/{domain_id}/test",
    auth=authenticated,
    response={201: dict},
    tags=["Domains"],
)
def domains_test(request: HttpRequest, organization_id: uuid.UUID, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    domain = scoped_object(Domain, organization, domain_id)
    try:
        test, address = create_domain_test(domain)
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
    record_api_audit(request, organization, "domain.test_created", test)
    return Status(
        201,
        {
            "id": str(test.id),
            "address": address,
            "expires_at": test.expires_at,
            "status": test.status,
        },
    )


@api.post(
    "/organizations/{organization_id}/domains/{domain_id}/disable",
    auth=authenticated,
    response={202: dict},
    tags=["Domains"],
)
def domains_disable(request: HttpRequest, organization_id: uuid.UUID, domain_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    domain = scoped_object(Domain, organization, domain_id)
    domain.status = Domain.Status.DISABLED
    domain.inbound_ready = False
    domain.outbound_ready = False
    domain.save(update_fields=("status", "inbound_ready", "outbound_ready", "updated_at"))
    domain.inbound_routes.update(is_active=False)
    job = enqueue_job(
        kind="reconcile_receipt_rule",
        idempotency_key=f"receipt-rule:disable:{domain.id}",
        payload={},
        organization=organization,
    )
    record_api_audit(request, organization, "domain.disabled", domain)
    return Status(202, {"status": domain.status, "job_id": str(job.id)})


@api.get(
    "/organizations/{organization_id}/conversations", auth=authenticated, tags=["Conversations"]
)
def conversations_list(
    request: HttpRequest,
    organization_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
    project_id: uuid.UUID | None = None,
    state: str | None = None,
    classification: str | None = None,
    q: str | None = None,
):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    queryset = Conversation.objects.filter(organization=organization)
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    if state in Conversation.Status.values:
        queryset = queryset.filter(status=state)
    if classification in Classification.Category.values:
        queryset = queryset.filter(
            messages__classifications__is_current=True,
            messages__classifications__category=classification,
        ).distinct()
    if q:
        queryset = queryset.filter(
            Q(subject__icontains=q)
            | Q(messages__from_address__icontains=q)
            | Q(messages__text_body__icontains=q)
        ).distinct()
    items, next_cursor = _paginate_queryset(
        queryset,
        cursor=cursor,
        limit=limit,
        timestamp_field="last_message_at",
        collection="conversations",
    )
    return {
        "items": [_conversation_dict(item) for item in items],
        "next_cursor": next_cursor,
    }


@api.get(
    "/organizations/{organization_id}/conversations/{conversation_id}",
    auth=authenticated,
    tags=["Conversations"],
)
def conversations_detail(
    request: HttpRequest, organization_id: uuid.UUID, conversation_id: uuid.UUID
):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    conversation = scoped_object(Conversation, organization, conversation_id)
    return _conversation_dict(conversation, details=True)


@api.post(
    "/organizations/{organization_id}/conversations/{conversation_id}/state",
    auth=authenticated,
    tags=["Conversations"],
)
def conversations_state(
    request: HttpRequest,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationStatusInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    conversation = scoped_object(Conversation, organization, conversation_id)
    conversation.status = payload.status
    conversation.resolved_at = (
        timezone.now() if payload.status == Conversation.Status.RESOLVED else None
    )
    conversation.save(update_fields=("status", "resolved_at", "updated_at"))
    record_api_audit(
        request,
        organization,
        "conversation.state_changed",
        conversation,
        {"status": conversation.status},
    )
    return _conversation_dict(conversation)


@api.post(
    "/organizations/{organization_id}/messages/{message_id}/classification",
    auth=authenticated,
    response={201: dict},
    tags=["Classifications"],
)
def classifications_override(
    request: HttpRequest,
    organization_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: ClassificationInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    message = scoped_object(Message, organization, message_id)
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
            organization=organization,
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
        organization,
        "classification.overridden",
        classification,
        {"category": classification.category, "urgency": classification.urgency},
    )
    return Status(201, {"id": str(classification.id), "category": classification.category})


@api.post(
    "/organizations/{organization_id}/conversations/{conversation_id}/drafts",
    auth=authenticated,
    response={201: dict},
    tags=["Drafts"],
)
def drafts_generate(request: HttpRequest, organization_id: uuid.UUID, conversation_id: uuid.UUID):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    conversation = scoped_object(Conversation, organization, conversation_id)
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
    record_api_audit(request, organization, "draft.generated", draft)
    return Status(
        201,
        {
            "id": str(draft.id),
            "revision_id": str(draft.current_revision_id),
            "content_hash": draft.current_revision.content_hash,
        },
    )


@api.get(
    "/organizations/{organization_id}/drafts/{draft_id}",
    auth=authenticated,
    tags=["Drafts"],
)
def drafts_detail(request: HttpRequest, organization_id: uuid.UUID, draft_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    draft = scoped_object(ReplyDraft, organization, draft_id)
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
    "/organizations/{organization_id}/drafts/{draft_id}/revisions",
    auth=authenticated,
    response={201: dict},
    tags=["Drafts"],
)
def drafts_revise(
    request: HttpRequest,
    organization_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: RevisionInput,
):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    draft = scoped_object(ReplyDraft, organization, draft_id)
    owner = organization.owner if isinstance(request.auth, APIToken) else request.user
    try:
        revision = revise_draft(
            draft=draft, owner=owner, subject=payload.subject, body_text=payload.body_text
        )
    except DjangoValidationError as exc:
        raise APIError("stale_draft", "; ".join(exc.messages), status=409) from exc
    record_api_audit(request, organization, "draft.revised", revision)
    return Status(
        201,
        {
            "id": str(revision.id),
            "number": revision.number,
            "content_hash": revision.content_hash,
        },
    )


@api.post(
    "/organizations/{organization_id}/drafts/{draft_id}/approval",
    auth=authenticated,
    response={202: dict},
    tags=["Drafts"],
)
def drafts_approve(
    request: HttpRequest,
    organization_id: uuid.UUID,
    draft_id: uuid.UUID,
    payload: ApprovalInput,
):
    require_scope(request, APIToken.Scope.APPROVE_SEND)
    organization = api_organization(request, organization_id)
    draft = scoped_object(ReplyDraft, organization, draft_id)
    owner = organization.owner if isinstance(request.auth, APIToken) else request.user
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
        organization=organization,
    )
    record_api_audit(
        request,
        organization,
        "draft.approved_and_queued",
        outbound,
        {"revision_id": str(payload.revision_id)},
    )
    return Status(202, {"outbound_id": str(outbound.id), "status": outbound.status})


@api.get(
    "/organizations/{organization_id}/outbound/{outbound_id}",
    auth=authenticated,
    tags=["Outbound"],
)
def outbound_status(request: HttpRequest, organization_id: uuid.UUID, outbound_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    outbound = scoped_object(OutboundMessage, organization, outbound_id)
    return {
        "id": str(outbound.id),
        "status": outbound.status,
        "attempt": outbound.attempt_number,
        "accepted_at": outbound.accepted_at,
        "delivered_at": outbound.delivered_at,
        "error": (
            {"code": outbound.error_code, "message": outbound.error_message}
            if outbound.error_code
            else None
        ),
    }


@api.post(
    "/organizations/{organization_id}/outbound/{outbound_id}/resend",
    auth=authenticated,
    response={202: dict},
    tags=["Outbound"],
)
def outbound_resend(request: HttpRequest, organization_id: uuid.UUID, outbound_id: uuid.UUID):
    require_scope(request, APIToken.Scope.APPROVE_SEND)
    organization = api_organization(request, organization_id)
    original = scoped_object(OutboundMessage, organization, outbound_id)
    owner = organization.owner if isinstance(request.auth, APIToken) else request.user
    try:
        resend = resend_outbound(original, owner=owner)
    except DjangoValidationError as exc:
        raise APIError("resend_not_allowed", "; ".join(exc.messages), status=409) from exc
    enqueue_job(
        kind="send_outbound",
        idempotency_key=f"outbound:{resend.id}",
        payload={"outbound_id": str(resend.id)},
        organization=organization,
    )
    record_api_audit(
        request,
        organization,
        "outbound.resend_queued",
        resend,
        {"original_id": str(original.id)},
    )
    return Status(202, {"outbound_id": str(resend.id), "status": resend.status})


@api.get("/organizations/{organization_id}/reports", auth=authenticated, tags=["Reports"])
def reports_list(
    request: HttpRequest,
    organization_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    reports, next_cursor = _paginate_queryset(
        Report.objects.filter(organization=organization),
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


@api.get(
    "/organizations/{organization_id}/notifications", auth=authenticated, tags=["Notifications"]
)
def notifications_list(
    request: HttpRequest,
    organization_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    notifications, next_cursor = _paginate_queryset(
        Notification.objects.filter(organization=organization, channel=Notification.Channel.IN_APP),
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
    "/organizations/{organization_id}/notifications/{notification_id}/read",
    auth=authenticated,
    tags=["Notifications"],
)
def notifications_read(
    request: HttpRequest, organization_id: uuid.UUID, notification_id: uuid.UUID
):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    notification = scoped_object(Notification, organization, notification_id)
    notification.status = Notification.Status.READ
    notification.read_at = timezone.now()
    notification.save(update_fields=("status", "read_at", "updated_at"))
    record_api_audit(request, organization, "notification.read", notification)
    return {"status": "read"}


@api.get("/organizations/{organization_id}/audit", auth=authenticated, tags=["Audit"])
def audit_list(
    request: HttpRequest,
    organization_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    events, next_cursor = _paginate_queryset(
        AuditEvent.objects.filter(organization=organization),
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
    "/organizations/{organization_id}/attachments/{attachment_id}/url",
    auth=authenticated,
    tags=["Attachments"],
)
def attachments_url(request: HttpRequest, organization_id: uuid.UUID, attachment_id: uuid.UUID):
    require_scope(request, APIToken.Scope.READ)
    organization = api_organization(request, organization_id)
    attachment = scoped_object(Attachment, organization, attachment_id)
    try:
        authorized = authorized_attachment_url(attachment=attachment, organization=organization)
    except AttachmentGoneError as exc:
        raise APIError("attachment_expired", str(exc), status=410) from exc
    except AttachmentLockedError as exc:
        raise APIError("attachment_locked", str(exc), status=423) from exc
    record_api_audit(request, organization, "attachment.url_issued", attachment)
    return {"url": authorized.url, "expires_in": authorized.expires_in}


@api.post(
    "/organizations/{organization_id}/tokens",
    auth=authenticated,
    response={201: dict},
    tags=["Tokens"],
)
def tokens_create(request: HttpRequest, organization_id: uuid.UUID, payload: TokenInput):
    require_scope(request, APIToken.Scope.WRITE)
    organization = api_organization(request, organization_id)
    if isinstance(request.auth, APIToken):
        raise APIError(
            "session_required", "API tokens must be created from an owner session.", status=403
        )
    try:
        token, raw = APIToken.issue(
            organization=organization,
            owner=request.user,
            name=payload.name,
            scopes=list(payload.scopes),
        )
    except DjangoValidationError as exc:
        raise APIError("validation_error", "; ".join(exc.messages)) from exc
    record_api_audit(
        request,
        organization,
        "api_token.created",
        token,
        {"scopes": token.scopes},
    )
    return Status(
        201,
        {
            "id": str(token.id),
            "token": raw,
            "prefix": token.prefix,
            "scopes": token.scopes,
            "shown_once": True,
        },
    )
