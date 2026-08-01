from __future__ import annotations

import uuid
from datetime import timedelta
from functools import wraps
from ipaddress import ip_address
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST

from inbox.forms import (
    APITokenForm,
    DomainForm,
    DraftRevisionForm,
    ProjectForm,
    RetentionForm,
    ScheduleForm,
    SignupForm,
    VerificationResendForm,
)
from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Classification,
    Conversation,
    Domain,
    EmailVerificationToken,
    Message,
    Notification,
    Organization,
    OutboundMessage,
    Project,
    ReplyDraft,
    Report,
    ReportSchedule,
    RetentionPolicy,
    SignupAttempt,
    User,
    token_digest,
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
from inbox.services.tenancy import current_organization, get_owned_organization, tenant_get_or_404


def _unique_slug(model, *, organization=None, value: str) -> str:
    base = slugify(value)[:70] or "workspace"
    candidate = base
    index = 2
    queryset = model.objects.all()
    if organization is not None:
        queryset = queryset.filter(organization=organization)
    while queryset.filter(slug=candidate).exists():
        candidate = f"{base[:65]}-{index}"
        index += 1
    return candidate


def _signup_client_ip(request: HttpRequest) -> str:
    """Resolve a client address without trusting headers from an untrusted peer."""

    def normalized(value: str) -> str | None:
        try:
            return str(ip_address(value.strip()))
        except ValueError:
            return None

    remote = normalized(str(request.META.get("REMOTE_ADDR", "")))
    if remote not in settings.TRUSTED_PROXY_IPS:
        return remote or "unknown"

    real_ip = normalized(str(request.META.get("HTTP_X_REAL_IP", "")))
    if real_ip is not None:
        return real_ip

    forwarded = [
        candidate
        for value in str(request.META.get("HTTP_X_FORWARDED_FOR", "")).split(",")
        if (candidate := normalized(value)) is not None
    ]
    for candidate in reversed(forwarded):
        if candidate not in settings.TRUSTED_PROXY_IPS:
            return candidate
    return forwarded[0] if forwarded else (remote or "unknown")


def _audit(
    organization: Organization,
    request: HttpRequest,
    event_type: str,
    obj: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    AuditEvent.objects.create(
        organization=organization,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=request.user.id,
        event_type=event_type,
        object_type=obj.__class__.__name__,
        object_id=obj.id,
        request_id=getattr(request, "request_id", "web"),
        metadata=metadata or {},
    )


def verified_required(view):
    @wraps(view)
    @login_required
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any):
        if not request.user.is_email_verified:
            messages.error(request, "Verify your email address before using Operational Inbox.")
            return redirect("verification_sent")
        return view(request, *args, **kwargs)

    return wrapped


@require_GET
def health_live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "live"})


@require_GET
def health_ready(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "not_ready"}, status=503)
    return JsonResponse({"status": "ready"})


def home(request: HttpRequest) -> HttpResponse:
    return redirect("dashboard" if request.user.is_authenticated else "login")


@verified_required
@require_POST
def organization_switch(request: HttpRequest) -> HttpResponse:
    organization = get_owned_organization(request.user, request.POST.get("organization_id", ""))
    request.session["organization_id"] = str(organization.id)
    request.session.pop("project_id", None)
    _audit(organization, request, "organization.selected", organization)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("dashboard")
    return redirect(next_url)


def signup(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignupForm(request.POST or None)
    attempt = None
    if request.method == "POST":
        fingerprint = token_digest(f"signup-ip:{_signup_client_ip(request)}")
        email_hash = token_digest(
            f"signup-email:{request.POST.get('email', '').strip().casefold()}"
        )
        since = timezone.now() - timedelta(seconds=settings.SIGNUP_RATE_WINDOW_SECONDS)
        recent_attempts = SignupAttempt.objects.filter(
            kind=SignupAttempt.Kind.SIGNUP, created_at__gte=since
        ).filter(Q(fingerprint_hash=fingerprint) | Q(email_hash=email_hash))
        if recent_attempts.count() >= settings.SIGNUP_RATE_LIMIT:
            form.add_error(None, "Too many signup attempts. Try again later.")
            response = render(request, "registration/signup.html", {"form": form})
            response.status_code = 429
            response["Retry-After"] = str(settings.SIGNUP_RATE_WINDOW_SECONDS)
            return response
        attempt = SignupAttempt.objects.create(
            kind=SignupAttempt.Kind.SIGNUP,
            fingerprint_hash=fingerprint,
            email_hash=email_hash,
        )
    if request.method == "POST" and form.is_valid():
        assert attempt is not None
        with transaction.atomic():
            user = form.save(commit=False)
            user.email = form.cleaned_data["email"]
            user.is_active = False
            user.save()
            organization = Organization.objects.create(
                owner=user,
                name=form.cleaned_data["organization_name"],
                slug=_unique_slug(Organization, value=form.cleaned_data["organization_name"]),
                timezone=form.cleaned_data["timezone"],
            )
            Project.objects.create(
                organization=organization,
                name=form.cleaned_data["project_name"],
                slug=_unique_slug(
                    Project,
                    organization=organization,
                    value=form.cleaned_data["project_name"],
                ),
            )
            ReportSchedule.objects.create(organization=organization)
            RetentionPolicy.objects.create(organization=organization)
            _, raw = EmailVerificationToken.issue(user, timezone.now() + timedelta(hours=24))
            SignupAttempt.objects.filter(id=attempt.id).update(accepted=True)
        verify_url = request.build_absolute_uri(reverse("verify_email", args=[raw]))
        try:
            send_mail(
                "Verify your Operational Inbox email",
                f"Verify your email address within 24 hours:\n\n{verify_url}",
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
            )
        except Exception:
            messages.warning(
                request,
                "The account was created, but verification delivery is delayed. "
                "Use the resend form to request a new link.",
            )
        request.session["verification_email"] = user.email
        return redirect("verification_sent")
    return render(request, "registration/signup.html", {"form": form})


def verification_sent(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "registration/verification_sent.html",
        {"verification_email": request.session.get("verification_email")},
    )


def verification_resend(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = VerificationResendForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        fingerprint = token_digest(f"verification-resend-ip:{_signup_client_ip(request)}")
        email_hash = token_digest(f"verification-resend-email:{email}")
        since = timezone.now() - timedelta(seconds=settings.VERIFICATION_RESEND_RATE_WINDOW_SECONDS)
        recent = SignupAttempt.objects.filter(
            kind=SignupAttempt.Kind.VERIFICATION_RESEND,
            created_at__gte=since,
        ).filter(Q(fingerprint_hash=fingerprint) | Q(email_hash=email_hash))
        if recent.count() >= settings.VERIFICATION_RESEND_RATE_LIMIT:
            form.add_error(None, "Too many verification requests. Try again later.")
            response = render(
                request,
                "registration/verification_resend.html",
                {"form": form},
            )
            response.status_code = 429
            response["Retry-After"] = str(settings.VERIFICATION_RESEND_RATE_WINDOW_SECONDS)
            return response
        attempt = SignupAttempt.objects.create(
            kind=SignupAttempt.Kind.VERIFICATION_RESEND,
            fingerprint_hash=fingerprint,
            email_hash=email_hash,
        )
        user = User.objects.filter(
            email=email,
            is_active=False,
            email_verified_at__isnull=True,
        ).first()
        if user is not None:
            with transaction.atomic():
                now = timezone.now()
                EmailVerificationToken.objects.filter(user=user, used_at__isnull=True).update(
                    used_at=now
                )
                _, raw = EmailVerificationToken.issue(user, now + timedelta(hours=24))
                SignupAttempt.objects.filter(id=attempt.id).update(accepted=True)
            verify_url = request.build_absolute_uri(reverse("verify_email", args=[raw]))
            try:
                send_mail(
                    "Verify your Operational Inbox email",
                    f"Verify your email address within 24 hours:\n\n{verify_url}",
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                )
            except Exception:
                messages.warning(
                    request,
                    "Verification delivery is delayed. You can safely retry later.",
                )
        request.session["verification_email"] = email
        messages.success(
            request,
            "If an unverified account exists for that address, a new link has been requested.",
        )
        return redirect("verification_sent")
    return render(request, "registration/verification_resend.html", {"form": form})


def verify_email(request: HttpRequest, token: str) -> HttpResponse:
    candidate = (
        EmailVerificationToken.objects.select_related("user")
        .filter(token_hash=token_digest(token))
        .first()
    )
    if candidate is None or not candidate.is_valid():
        return render(request, "registration/verification_invalid.html", status=400)
    with transaction.atomic():
        candidate.used_at = timezone.now()
        candidate.save(update_fields=("used_at", "updated_at"))
        user = candidate.user
        user.email_verified_at = timezone.now()
        user.is_active = True
        user.save(update_fields=("email_verified_at", "is_active"))
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    organization = user.organizations.first()
    if organization:
        request.session["organization_id"] = str(organization.id)
    messages.success(request, "Your email is verified. Welcome to Operational Inbox.")
    return redirect("dashboard")


def _selected_project(request: HttpRequest, organization: Organization) -> Project | None:
    selected_id = request.GET.get("project") or request.session.get("project_id")
    selected = (
        Project.objects.filter(organization=organization, id=selected_id, is_active=True).first()
        if selected_id
        else None
    )
    selected = selected or Project.objects.filter(organization=organization, is_active=True).first()
    if selected:
        request.session["project_id"] = str(selected.id)
    return selected


@verified_required
def dashboard(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    project = _selected_project(request, organization)
    conversations = Conversation.objects.filter(organization=organization)
    messages_qs = Message.objects.filter(organization=organization)
    domains = Domain.objects.filter(organization=organization)
    if project:
        conversations = conversations.filter(project=project)
        messages_qs = messages_qs.filter(project=project)
        domains = domains.filter(project=project)
    attention = (
        conversations.filter(
            Q(messages__classifications__is_current=True)
            & (
                Q(messages__classifications__category=Classification.Category.ACTIONABLE)
                | Q(messages__classifications__category=Classification.Category.SUSPICIOUS)
                | Q(messages__classifications__urgency__in=["HIGH", "CRITICAL"])
            )
        )
        .distinct()
        .order_by("-last_message_at")[:8]
    )
    context = {
        "active_nav": "overview",
        "project": project,
        "projects": Project.objects.filter(organization=organization, is_active=True),
        "attention": attention,
        "metrics": {
            "open": conversations.filter(status=Conversation.Status.OPEN).count(),
            "quarantined": messages_qs.filter(is_quarantined=True).count(),
            "unclassified": messages_qs.exclude(classifications__is_current=True).count(),
            "domains_ready": domains.filter(status=Domain.Status.READY).count(),
            "domains_total": domains.exclude(status=Domain.Status.DISABLED).count(),
        },
        "domains": domains.order_by("hostname")[:4],
        "reports": Report.objects.filter(organization=organization)[:4],
        "audit_events": AuditEvent.objects.filter(organization=organization)[:6],
    }
    return render(request, "inbox/dashboard.html", context)


@verified_required
def inbox_list(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    project = _selected_project(request, organization)
    conversations = Conversation.objects.filter(organization=organization).annotate(
        message_count=Count("messages")
    )
    if project:
        conversations = conversations.filter(project=project)
    query = request.GET.get("q", "").strip()
    if query:
        conversations = conversations.filter(
            Q(subject__icontains=query)
            | Q(messages__from_address__icontains=query)
            | Q(messages__text_body__icontains=query)
        ).distinct()
    state = request.GET.get("state", "")
    if state in Conversation.Status.values:
        conversations = conversations.filter(status=state)
    classification = request.GET.get("classification", "")
    if classification in Classification.Category.values:
        conversations = conversations.filter(
            messages__classifications__is_current=True,
            messages__classifications__category=classification,
        ).distinct()
    security = request.GET.get("security", "")
    if security == "suspicious":
        conversations = conversations.filter(messages__is_suspicious=True).distinct()
    elif security == "quarantined":
        conversations = conversations.filter(messages__is_quarantined=True).distinct()
    ordered_conversations = conversations.select_related("project").order_by("-last_message_at")
    paginator = Paginator(ordered_conversations, 50)
    page = paginator.get_page(request.GET.get("page"))
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    return render(
        request,
        "inbox/inbox_list.html",
        {
            "active_nav": "inbox",
            "project": project,
            "projects": Project.objects.filter(organization=organization, is_active=True),
            "conversations": page.object_list,
            "page_obj": page,
            "pagination_query": pagination_params.urlencode(),
            "filters": {
                "q": query,
                "state": state,
                "classification": classification,
                "security": security,
            },
            "conversation_states": Conversation.Status.choices,
            "classification_categories": Classification.Category.choices,
        },
    )


@verified_required
def conversation_detail(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    conversation = tenant_get_or_404(
        Conversation.objects.select_related("project"),
        organization=organization,
        id=conversation_id,
    )
    draft = (
        conversation.reply_drafts.select_related("current_revision").order_by("-created_at").first()
    )
    return render(
        request,
        "inbox/conversation_detail.html",
        {
            "active_nav": "inbox",
            "conversation": conversation,
            "timeline": conversation.messages.prefetch_related(
                "recipients", "attachments", "classifications", "delivery_events"
            ),
            "draft": draft,
            "draft_form": DraftRevisionForm(
                initial=(
                    {
                        "subject": draft.current_revision.subject,
                        "body_text": draft.current_revision.body_text,
                    }
                    if draft and draft.current_revision
                    else None
                )
            ),
            "outbound_messages": conversation.outbound_messages.order_by("-created_at"),
        },
    )


@verified_required
@require_POST
def conversation_status(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    conversation = tenant_get_or_404(
        Conversation.objects, organization=organization, id=conversation_id
    )
    status = request.POST.get("status", "")
    if status not in Conversation.Status.values or status == Conversation.Status.QUARANTINED:
        messages.error(request, "Select a supported conversation state.")
    else:
        conversation.status = status
        conversation.resolved_at = (
            timezone.now() if status == Conversation.Status.RESOLVED else None
        )
        conversation.save(update_fields=("status", "resolved_at", "updated_at"))
        _audit(
            organization, request, "conversation.status_changed", conversation, {"status": status}
        )
        messages.success(request, "Conversation status updated.")
    return redirect("conversation_detail", conversation_id=conversation.id)


@verified_required
@require_POST
def draft_generate(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    conversation = tenant_get_or_404(
        Conversation.objects, organization=organization, id=conversation_id
    )
    message = conversation.messages.filter(direction=Message.Direction.INBOUND).last()
    if message is None:
        messages.error(request, "No inbound message is available for drafting.")
    else:
        try:
            draft = create_draft(message)
        except Exception:
            messages.error(
                request,
                "A draft could not be generated. The message remains available and unmodified.",
            )
        else:
            _audit(organization, request, "draft.generated", draft)
            messages.success(request, "Draft generated for human review.")
    return redirect("conversation_detail", conversation_id=conversation.id)


@verified_required
@require_POST
def draft_revise(request: HttpRequest, draft_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    draft = tenant_get_or_404(
        ReplyDraft.objects.select_related("conversation"), organization=organization, id=draft_id
    )
    form = DraftRevisionForm(request.POST)
    if form.is_valid():
        try:
            revision = revise_draft(draft=draft, owner=request.user, **form.cleaned_data)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            _audit(organization, request, "draft.revised", revision, {"number": revision.number})
            messages.success(request, "A new immutable draft revision was created.")
    else:
        messages.error(request, "Correct the draft fields and try again.")
    return redirect("conversation_detail", conversation_id=draft.conversation_id)


@verified_required
@require_POST
def draft_approve(request: HttpRequest, draft_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    draft = tenant_get_or_404(
        ReplyDraft.objects.select_related("conversation"), organization=organization, id=draft_id
    )
    try:
        outbound = approve_exact_revision(
            draft=draft,
            revision_id=uuid.UUID(request.POST.get("revision_id", "")),
            content_hash=request.POST.get("content_hash", ""),
            owner=request.user,
        )
    except (ValidationError, ValueError) as exc:
        messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
    else:
        enqueue_job(
            kind="send_outbound",
            idempotency_key=f"outbound:{outbound.id}",
            payload={"outbound_id": str(outbound.id)},
            organization=organization,
        )
        _audit(organization, request, "draft.approved_exact_revision", outbound)
        messages.success(request, "The exact revision was approved and queued for sending.")
    return redirect("conversation_detail", conversation_id=draft.conversation_id)


@verified_required
@require_POST
def outbound_resend(request: HttpRequest, outbound_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    original = tenant_get_or_404(
        OutboundMessage.objects.select_related("conversation", "revision"),
        organization=organization,
        id=outbound_id,
    )
    try:
        resend = resend_outbound(original, owner=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        enqueue_job(
            kind="send_outbound",
            idempotency_key=f"outbound:{resend.id}",
            payload={"outbound_id": str(resend.id)},
            organization=organization,
        )
        _audit(organization, request, "outbound.explicit_resend", resend)
        messages.success(request, "A distinct resend attempt was queued.")
    return redirect("conversation_detail", conversation_id=original.conversation_id)


@verified_required
def domains_list(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    return render(
        request,
        "inbox/domains_list.html",
        {
            "active_nav": "domains",
            "domains": Domain.objects.filter(organization=organization).select_related("project"),
            "limit": settings.MAX_DOMAINS_PER_ORGANIZATION,
        },
    )


@verified_required
def domain_create_view(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    project = _selected_project(request, organization)
    if project is None:
        raise Http404
    form = DomainForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            domain = create_domain(
                organization=organization,
                project=project,
                hostname=form.cleaned_data["hostname"],
                setup_mode=form.cleaned_data["setup_mode"],
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            enqueue_job(
                kind="provision_domain",
                idempotency_key=f"provision-domain:{domain.id}",
                payload={"domain_id": str(domain.id)},
                organization=organization,
            )
            _audit(organization, request, "domain.claim_created", domain)
            messages.success(request, "Domain claim created. Provisioning has been queued.")
            return redirect("domain_detail", domain_id=domain.id)
    return render(
        request,
        "inbox/domain_create.html",
        {"active_nav": "domains", "form": form, "project": project},
    )


@verified_required
def domain_detail(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    domain = tenant_get_or_404(
        Domain.objects.select_related("project").prefetch_related("dns_records", "inbound_routes"),
        organization=organization,
        id=domain_id,
    )
    return render(
        request,
        "inbox/domain_detail.html",
        {
            "active_nav": "domains",
            "domain": domain,
            "new_test_address": request.session.pop(f"domain_test_address:{domain.id}", None),
        },
    )


@verified_required
@require_POST
def domain_create_test(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    domain = tenant_get_or_404(Domain.objects, organization=organization, id=domain_id)
    _, address = create_domain_test(domain)
    request.session[f"domain_test_address:{domain.id}"] = address
    _audit(organization, request, "domain.test_created", domain)
    messages.success(request, "A new 24-hour test-delivery address was created.")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_disable(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    domain = tenant_get_or_404(Domain.objects, organization=organization, id=domain_id)
    domain.status = Domain.Status.DISABLED
    domain.inbound_ready = False
    domain.outbound_ready = False
    domain.save(update_fields=("status", "inbound_ready", "outbound_ready", "updated_at"))
    domain.inbound_routes.update(is_active=False)
    enqueue_job(
        kind="reconcile_receipt_rule",
        idempotency_key=f"receipt-rule:disable:{domain.id}",
        payload={},
        organization=organization,
    )
    _audit(organization, request, "domain.disabled", domain)
    messages.success(request, "The domain and its inbound routes were disabled.")
    return redirect("domains")


@verified_required
def projects(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    form = ProjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if (
            Project.objects.filter(organization=organization, is_active=True).count()
            >= settings.MAX_PROJECTS_PER_ORGANIZATION
        ):
            form.add_error(None, "The organization has reached its project limit.")
        else:
            project = form.save(commit=False)
            project.organization = organization
            project.slug = _unique_slug(Project, organization=organization, value=project.name)
            project.save()
            _audit(organization, request, "project.created", project)
            messages.success(request, "Project created.")
            return redirect("projects")
    return render(
        request,
        "inbox/projects.html",
        {
            "active_nav": "projects",
            "form": form,
            "projects": Project.objects.filter(organization=organization).order_by("name"),
            "limit": settings.MAX_PROJECTS_PER_ORGANIZATION,
        },
    )


@verified_required
def reports_list(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    return render(
        request,
        "inbox/reports.html",
        {"active_nav": "reports", "reports": Report.objects.filter(organization=organization)},
    )


@verified_required
def notifications_list(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    if request.method == "POST":
        Notification.objects.filter(
            organization=organization,
            channel=Notification.Channel.IN_APP,
            read_at__isnull=True,
        ).update(status=Notification.Status.READ, read_at=timezone.now())
        return redirect("notifications")
    return render(
        request,
        "inbox/notifications.html",
        {
            "active_nav": "notifications",
            "notifications": Notification.objects.filter(
                organization=organization, channel=Notification.Channel.IN_APP
            ),
        },
    )


@verified_required
def schedules_settings(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    schedule, _ = ReportSchedule.objects.get_or_create(organization=organization)
    retention, _ = RetentionPolicy.objects.get_or_create(organization=organization)
    schedule_form = ScheduleForm(
        request.POST or None,
        instance=schedule,
        organization=organization,
        prefix="schedule",
    )
    retention_form = RetentionForm(request.POST or None, instance=retention, prefix="retention")
    if request.method == "POST":
        if request.POST.get("form") == "schedule" and schedule_form.is_valid():
            schedule_form.save()
            organization.timezone = schedule_form.cleaned_data["timezone"]
            organization.save(update_fields=("timezone", "updated_at"))
            _audit(organization, request, "schedule.updated", schedule)
            messages.success(request, "Review schedule updated.")
            return redirect("schedules_settings")
        if request.POST.get("form") == "retention" and retention_form.is_valid():
            retention_form.save()
            _audit(organization, request, "retention.updated", retention)
            messages.success(request, "Retention policy updated.")
            return redirect("schedules_settings")
    return render(
        request,
        "inbox/settings_schedules.html",
        {
            "active_nav": "schedules",
            "schedule_form": schedule_form,
            "retention_form": retention_form,
        },
    )


@verified_required
def api_tokens(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    form = APITokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token, raw = APIToken.issue(
            organization=organization,
            owner=request.user,
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
        )
        request.session["new_api_token"] = raw
        _audit(organization, request, "api_token.created", token, {"scopes": token.scopes})
        return redirect("api_tokens")
    new_token = request.session.pop("new_api_token", None)
    return render(
        request,
        "inbox/api_tokens.html",
        {
            "active_nav": "api_tokens",
            "form": form,
            "tokens": APIToken.objects.filter(organization=organization).order_by("-created_at"),
            "new_token": new_token,
        },
    )


@verified_required
@require_POST
def api_token_revoke(request: HttpRequest, token_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    token = tenant_get_or_404(APIToken.objects, organization=organization, id=token_id)
    token.revoked_at = timezone.now()
    token.save(update_fields=("revoked_at", "updated_at"))
    _audit(organization, request, "api_token.revoked", token)
    messages.success(request, "API token revoked.")
    return redirect("api_tokens")


@verified_required
def audit_log(request: HttpRequest) -> HttpResponse:
    organization = current_organization(request)
    return render(
        request,
        "inbox/audit.html",
        {
            "active_nav": "audit",
            "audit_events": AuditEvent.objects.filter(organization=organization)[:250],
        },
    )


@verified_required
def attachment_download(request: HttpRequest, attachment_id: uuid.UUID) -> HttpResponse:
    organization = current_organization(request)
    attachment = tenant_get_or_404(Attachment.objects, organization=organization, id=attachment_id)
    try:
        authorized = authorized_attachment_url(attachment=attachment, organization=organization)
    except AttachmentGoneError as exc:
        return render(
            request, "inbox/attachment_unavailable.html", {"reason": str(exc)}, status=410
        )
    except AttachmentLockedError as exc:
        return render(
            request, "inbox/attachment_unavailable.html", {"reason": str(exc)}, status=423
        )
    _audit(organization, request, "attachment.download_authorized", attachment)
    return redirect(authorized.url)
