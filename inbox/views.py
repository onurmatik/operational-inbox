from __future__ import annotations

import hashlib
import logging
import posixpath
import uuid
from datetime import timedelta
from functools import wraps
from ipaddress import ip_address
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, send_mail
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from sesame.utils import get_parameters
from sesame.views import LoginView as SesameLoginView

from inbox.forms import (
    APITokenForm,
    DomainForm,
    DraftRevisionForm,
    RetentionForm,
    ScheduleForm,
    SignupForm,
    StartOnboardingForm,
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
    OutboundMessage,
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
from inbox.services.domains import (
    DomainClaimConflict,
    create_domain,
    create_domain_test,
    inspect_mx,
    normalize_hostname,
)
from inbox.services.drafts import (
    approve_exact_revision,
    create_draft,
    resend_outbound,
    revise_draft,
)
from inbox.services.jobs import (
    can_retry_domain_provisioning,
    enqueue_job,
    request_outbound_provisioning,
    retry_domain_provisioning,
)
from inbox.services.tenancy import current_domain, domain_get_or_404, get_owned_domain

logger = logging.getLogger(__name__)

PENDING_DOMAIN_SESSION_KEY = "pending_domain"
MAGIC_LINK_SCOPE = "operational-inbox-login"
MAGIC_LINK_MAX_AGE_SECONDS = 10 * 60
ONBOARDING_STATE_SALT = "operational-inbox-onboarding-v1"
DOMAIN_TEST_SESSION_PREFIX = "domain_test_address:"


def _domain_test_session_key(domain_id: uuid.UUID) -> str:
    return f"{DOMAIN_TEST_SESSION_PREFIX}{domain_id}"


def _active_domain_test_address(request: HttpRequest, domain: Domain) -> str | None:
    key = _domain_test_session_key(domain.id)
    if domain.status in {Domain.Status.READY, Domain.Status.ERROR, Domain.Status.DISABLED}:
        request.session.pop(key, None)
        return None
    payload = request.session.get(key)
    if not isinstance(payload, dict):
        request.session.pop(key, None)
        return None
    address = payload.get("address")
    expires_at = payload.get("expires_at")
    if (
        not isinstance(address, str)
        or not address
        or not isinstance(expires_at, (int, float))
        or expires_at <= timezone.now().timestamp()
    ):
        request.session.pop(key, None)
        return None
    return address


def _safe_next(request: HttpRequest, value: str | None) -> str:
    """Return a normalized in-app URL or the appropriate onboarding default."""

    fallback = (
        reverse("domain_create")
        if request.session.get(PENDING_DOMAIN_SESSION_KEY)
        else reverse("dashboard")
    )
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlsplit(value)
    decoded_path = parsed.path
    for _ in range(3):
        next_decoded_path = unquote(decoded_path)
        if next_decoded_path == decoded_path:
            break
        decoded_path = next_decoded_path
    normalized_path = posixpath.normpath(decoded_path)
    if "\\" in decoded_path or not (
        normalized_path == "/app" or normalized_path.startswith("/app/")
    ):
        return fallback
    if not url_has_allowed_host_and_scheme(
        value,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return fallback
    return value


def _take_rate_limit_slot(
    *, kind: str, fingerprint_hash: str, email_hash: str, limit: int, window_seconds: int
) -> SignupAttempt | None:
    """Atomically reserve one request slot in the durable sliding window."""

    since = timezone.now() - timedelta(seconds=window_seconds)
    with transaction.atomic():
        recent_attempts = SignupAttempt.objects.filter(
            kind=kind,
            created_at__gte=since,
        ).filter(Q(fingerprint_hash=fingerprint_hash) | Q(email_hash=email_hash))
        if recent_attempts.count() >= limit:
            return None
        return SignupAttempt.objects.create(
            kind=kind,
            fingerprint_hash=fingerprint_hash,
            email_hash=email_hash,
        )


def _signed_onboarding_state(user: User, pending_domain: str | None) -> str:
    if not pending_domain:
        return ""
    return signing.dumps(
        {"user_id": str(user.pk), "domain": pending_domain},
        salt=ONBOARDING_STATE_SALT,
        compress=True,
    )


def _restore_onboarding_state(request: HttpRequest, user: User) -> None:
    value = request.GET.get("onboarding", "")
    if not value:
        return
    try:
        payload = signing.loads(
            value,
            salt=ONBOARDING_STATE_SALT,
            max_age=MAGIC_LINK_MAX_AGE_SECONDS,
        )
        if payload.get("user_id") != str(user.pk):
            return
        pending_domain = normalize_hostname(str(payload.get("domain", "")))
    except (AttributeError, signing.BadSignature, ValidationError):
        return
    request.session[PENDING_DOMAIN_SESSION_KEY] = pending_domain


def _prepare_magic_link_user(email: str, pending_domain: str | None) -> User | None:
    """Create/reactivate an eligible user without reviving a verified disabled account."""

    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=email).first()
        if user is None:
            user = User(email=email, is_active=True)
            user.set_unusable_password()
            user.save()
        elif user.is_staff or user.is_superuser:
            return None
        elif not user.is_active:
            if user.email_verified_at is not None:
                return None
            user.is_active = True
            user.set_unusable_password()
            user.save(update_fields=("is_active", "password"))
    return user


def _auth_error_message(value: str) -> str:
    if value == "invalid-link":
        return "That sign-in link is invalid or has expired. Request a new link below."
    return ""


def _send_branded_auth_email(
    *,
    recipient: str,
    subject: str,
    text_template: str,
    html_template: str,
    context: dict[str, Any],
) -> None:
    message = EmailMultiAlternatives(
        subject,
        render_to_string(text_template, context),
        settings.DEFAULT_FROM_EMAIL,
        [recipient],
    )
    message.attach_alternative(render_to_string(html_template, context), "text/html")
    if message.send() != 1:
        raise RuntimeError("Authentication email backend reported no delivery.")


def _signup_context(
    request: HttpRequest,
    *,
    form: SignupForm,
    sent: bool = False,
    email: str = "",
    auth_error: str = "",
) -> dict[str, Any]:
    return {
        "form": form,
        "sent": sent,
        "email": email,
        "auth_error": auth_error,
        "pending_domain": request.session.get(PENDING_DOMAIN_SESSION_KEY),
        "next": _safe_next(request, request.POST.get("next") or request.GET.get("next")),
    }


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
    domain: Domain,
    request: HttpRequest,
    event_type: str,
    obj: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    AuditEvent.objects.create(
        domain=domain,
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


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("dashboard")
    hostname = request.session.get(PENDING_DOMAIN_SESSION_KEY, "")
    return render(
        request,
        "onboarding/landing.html",
        {
            "form": StartOnboardingForm(initial={"hostname": hostname}),
            "hostname": hostname,
            "domain_error": "",
        },
    )


@require_POST
def start_onboarding(request: HttpRequest) -> HttpResponse:
    form = StartOnboardingForm(request.POST)
    if not form.is_valid():
        domain_error = form.errors.get("hostname", ["Enter a valid domain name."])[0]
        return render(
            request,
            "onboarding/landing.html",
            {
                "form": form,
                "hostname": request.POST.get("hostname", ""),
                "domain_error": domain_error,
            },
            status=400,
        )
    request.session[PENDING_DOMAIN_SESSION_KEY] = form.cleaned_data["hostname"]
    return redirect("signup")


@verified_required
@require_POST
def domain_switch(request: HttpRequest) -> HttpResponse:
    domain = get_owned_domain(request.user, request.POST.get("domain_id", ""))
    request.session["domain_id"] = str(domain.id)
    _audit(domain, request, "domain.selected", domain)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("dashboard")
    return redirect(next_url)


@require_http_methods(["GET", "POST"])
def signup(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(_safe_next(request, request.GET.get("next")))
    form = SignupForm(request.POST or None)
    attempt = None
    if request.method == "POST":
        fingerprint = token_digest(f"signup-ip:{_signup_client_ip(request)}")
        email_hash = token_digest(
            f"signup-email:{request.POST.get('email', '').strip().casefold()}"
        )
        attempt = _take_rate_limit_slot(
            kind=SignupAttempt.Kind.SIGNUP,
            fingerprint_hash=fingerprint,
            email_hash=email_hash,
            limit=settings.SIGNUP_RATE_LIMIT,
            window_seconds=settings.SIGNUP_RATE_WINDOW_SECONDS,
        )
        if attempt is None:
            form.add_error(None, "Too many signup attempts. Try again later.")
            response = render(
                request,
                "registration/signup.html",
                _signup_context(
                    request,
                    form=form,
                    email=request.POST.get("email", "").strip(),
                ),
            )
            response.status_code = 429
            response["Retry-After"] = str(settings.SIGNUP_RATE_WINDOW_SECONDS)
            return response
    if request.method == "POST" and form.is_valid():
        assert attempt is not None
        email = form.cleaned_data["email"]
        prepared = _prepare_magic_link_user(
            email,
            request.session.get(PENDING_DOMAIN_SESSION_KEY),
        )
        logo_url = urljoin(
            f"{settings.PUBLIC_BASE_URL}/",
            f"{settings.STATIC_URL.lstrip('/')}img/logo.svg",
        )
        if prepared is None:
            try:
                _send_branded_auth_email(
                    recipient=email,
                    subject="Your Operational Inbox sign-in request",
                    text_template="email/magic_link_unavailable.txt",
                    html_template="email/magic_link_unavailable.html",
                    context={"logo_url": logo_url},
                )
            except Exception:
                logger.exception("Magic-link delivery failed")
                form.add_error(None, "We could not send the link. Try again in a moment.")
                return render(
                    request,
                    "registration/signup.html",
                    _signup_context(request, form=form, email=email),
                    status=503,
                )
            SignupAttempt.objects.filter(id=attempt.id).update(accepted=True)
            return render(
                request,
                "registration/signup.html",
                _signup_context(request, form=form, sent=True, email=email),
            )

        user = prepared
        next_url = _safe_next(request, request.POST.get("next"))
        parameters = get_parameters(user, scope=MAGIC_LINK_SCOPE)
        parameters["next"] = next_url
        onboarding_state = _signed_onboarding_state(
            user,
            request.session.get(PENDING_DOMAIN_SESSION_KEY),
        )
        if onboarding_state:
            parameters["onboarding"] = onboarding_state
        callback_path = f"{reverse('sesame_login')}?{urlencode(parameters)}"
        login_url = urljoin(f"{settings.PUBLIC_BASE_URL}/", callback_path.lstrip("/"))
        email_context = {
            "login_url": login_url,
            "logo_url": logo_url,
            "pending_domain": request.session.get(PENDING_DOMAIN_SESSION_KEY),
        }
        try:
            _send_branded_auth_email(
                recipient=user.email,
                subject="Your Operational Inbox sign-in link",
                text_template="email/magic_link.txt",
                html_template="email/magic_link.html",
                context=email_context,
            )
        except Exception:
            logger.exception("Magic-link delivery failed")
            form.add_error(None, "We could not send the link. Try again in a moment.")
            return render(
                request,
                "registration/signup.html",
                _signup_context(request, form=form, email=email),
                status=503,
            )
        SignupAttempt.objects.filter(id=attempt.id).update(accepted=True)
        return render(
            request,
            "registration/signup.html",
            _signup_context(request, form=form, sent=True, email=email),
        )
    return render(
        request,
        "registration/signup.html",
        _signup_context(
            request,
            form=form,
            auth_error=_auth_error_message(request.GET.get("auth_error", "")),
        ),
        status=400 if request.method == "POST" else 200,
    )


@require_GET
def login_redirect(request: HttpRequest) -> HttpResponse:
    next_url = request.GET.get("next")
    if next_url:
        return redirect(f"{reverse('signup')}?{urlencode({'next': _safe_next(request, next_url)})}")
    return redirect("signup")


class OperationalInboxSesameLoginView(SesameLoginView):
    scope = MAGIC_LINK_SCOPE
    max_age = MAGIC_LINK_MAX_AGE_SECONDS

    def login_failed(self) -> HttpResponse:
        query = urlencode({"auth_error": "invalid-link"})
        return redirect(f"{reverse('signup')}?{query}")

    def login_success(self) -> HttpResponse:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=self.request.user.pk)
            if user.is_staff or user.is_superuser:
                logout(self.request)
                return self.login_failed()
            _restore_onboarding_state(self.request, user)
            if user.email_verified_at is None:
                user.email_verified_at = timezone.now()
                user.save(update_fields=("email_verified_at",))
        self.request.user = user
        selected = (
            user.domains.exclude(status=Domain.Status.DISABLED)
            .order_by("created_at")
            .first()
        )
        if selected:
            self.request.session["domain_id"] = str(selected.id)
        return redirect(_safe_next(self.request, self.request.GET.get("next")))


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
        attempt = _take_rate_limit_slot(
            kind=SignupAttempt.Kind.VERIFICATION_RESEND,
            fingerprint_hash=fingerprint,
            email_hash=email_hash,
            limit=settings.VERIFICATION_RESEND_RATE_LIMIT,
            window_seconds=settings.VERIFICATION_RESEND_RATE_WINDOW_SECONDS,
        )
        if attempt is None:
            form.add_error(None, "Too many verification requests. Try again later.")
            response = render(
                request,
                "registration/verification_resend.html",
                {"form": form},
            )
            response.status_code = 429
            response["Retry-After"] = str(settings.VERIFICATION_RESEND_RATE_WINDOW_SECONDS)
            return response
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
    domain = user.domains.exclude(status=Domain.Status.DISABLED).first()
    if domain:
        request.session["domain_id"] = str(domain.id)
    messages.success(request, "Your email is verified. Welcome to Operational Inbox.")
    return redirect("dashboard")


@verified_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if not request.user.domains.exclude(status=Domain.Status.DISABLED).exists():
        return redirect("domain_create")
    domain = current_domain(request)
    conversations = Conversation.objects.filter(domain=domain)
    messages_qs = Message.objects.filter(domain=domain)
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
        "domain": domain,
        "attention": attention,
        "metrics": {
            "open": conversations.filter(status=Conversation.Status.OPEN).count(),
            "quarantined": messages_qs.filter(is_quarantined=True).count(),
            "unclassified": messages_qs.exclude(classifications__is_current=True).count(),
            "domains_ready": int(domain.status == Domain.Status.READY),
            "domains_total": 1,
        },
        "domains": [domain],
        "reports": Report.objects.filter(domain=domain)[:4],
        "audit_events": AuditEvent.objects.filter(domain=domain)[:6],
    }
    return render(request, "inbox/dashboard.html", context)


@verified_required
def inbox_list(request: HttpRequest) -> HttpResponse:
    domain = current_domain(request)
    conversations = Conversation.objects.filter(domain=domain).annotate(
        message_count=Count("messages")
    )
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
    ordered_conversations = conversations.order_by("-last_message_at")
    paginator = Paginator(ordered_conversations, 50)
    page = paginator.get_page(request.GET.get("page"))
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    return render(
        request,
        "inbox/inbox_list.html",
        {
            "active_nav": "inbox",
            "domain": domain,
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
    domain = current_domain(request)
    conversation = domain_get_or_404(
        Conversation.objects,
        domain=domain,
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
    domain = current_domain(request)
    conversation = domain_get_or_404(
        Conversation.objects, domain=domain, id=conversation_id
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
            domain, request, "conversation.status_changed", conversation, {"status": status}
        )
        messages.success(request, "Conversation status updated.")
    return redirect("conversation_detail", conversation_id=conversation.id)


@verified_required
@require_POST
def draft_generate(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    conversation = domain_get_or_404(
        Conversation.objects, domain=domain, id=conversation_id
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
            _audit(domain, request, "draft.generated", draft)
            messages.success(request, "Draft generated for human review.")
    return redirect("conversation_detail", conversation_id=conversation.id)


@verified_required
@require_POST
def draft_revise(request: HttpRequest, draft_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    draft = domain_get_or_404(
        ReplyDraft.objects.select_related("conversation"), domain=domain, id=draft_id
    )
    form = DraftRevisionForm(request.POST)
    if form.is_valid():
        try:
            revision = revise_draft(draft=draft, owner=request.user, **form.cleaned_data)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            _audit(domain, request, "draft.revised", revision, {"number": revision.number})
            messages.success(request, "A new immutable draft revision was created.")
    else:
        messages.error(request, "Correct the draft fields and try again.")
    return redirect("conversation_detail", conversation_id=draft.conversation_id)


@verified_required
@require_POST
def draft_approve(request: HttpRequest, draft_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    draft = domain_get_or_404(
        ReplyDraft.objects.select_related("conversation"), domain=domain, id=draft_id
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
            domain=domain,
        )
        _audit(domain, request, "draft.approved_exact_revision", outbound)
        messages.success(request, "The exact revision was approved and queued for sending.")
    return redirect("conversation_detail", conversation_id=draft.conversation_id)


@verified_required
@require_POST
def outbound_resend(request: HttpRequest, outbound_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    original = domain_get_or_404(
        OutboundMessage.objects.select_related("conversation", "revision"),
        domain=domain,
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
            domain=domain,
        )
        _audit(domain, request, "outbound.explicit_resend", resend)
        messages.success(request, "A distinct resend attempt was queued.")
    return redirect("conversation_detail", conversation_id=original.conversation_id)


@verified_required
def domains_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "inbox/domains_list.html",
        {
            "active_nav": "domains",
            "domains": Domain.objects.filter(owner=request.user),
            "limit": settings.MAX_DOMAINS_PER_USER,
        },
    )


@never_cache
@verified_required
@require_POST
def domain_mx_inspect(request: HttpRequest) -> JsonResponse:
    try:
        hostname = normalize_hostname(request.POST.get("hostname", ""))
    except ValidationError as exc:
        return JsonResponse(
            {
                "code": "invalid_hostname",
                "message": "; ".join(exc.messages),
            },
            status=400,
        )

    cache_digest = hashlib.sha256(hostname.encode("ascii")).hexdigest()
    cache_key = f"domain-mx-inspection:v1:{cache_digest}"
    serialized_records = cache.get(cache_key)
    if serialized_records is None:
        try:
            mx_records = inspect_mx(hostname)
        except ValidationError as exc:
            return JsonResponse(
                {
                    "code": "mx_lookup_failed",
                    "message": "; ".join(exc.messages),
                },
                status=503,
            )
        serialized_records = [
            {"preference": record.preference, "exchange": record.exchange} for record in mx_records
        ]
        cache.set(cache_key, serialized_records, timeout=60)

    return JsonResponse(
        {
            "hostname": hostname,
            "has_existing_mx": bool(serialized_records),
            "recommended_setup_mode": (
                Domain.SetupMode.PROVIDER_FORWARD
                if serialized_records
                else Domain.SetupMode.DIRECT_MX
            ),
            "mx_records": serialized_records,
        }
    )


@verified_required
def domain_create_view(request: HttpRequest) -> HttpResponse:
    pending_domain = request.session.get(PENDING_DOMAIN_SESSION_KEY, "")
    form = DomainForm(
        request.POST or None,
        initial={"hostname": pending_domain} if pending_domain else None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            domain = create_domain(
                owner=request.user,
                hostname=form.cleaned_data["hostname"],
                setup_mode=form.cleaned_data["setup_mode"],
            )
        except DomainClaimConflict as exc:
            if exc.existing_domain is not None:
                same_configuration = (
                    exc.existing_domain.setup_mode == form.cleaned_data["setup_mode"]
                )
                if same_configuration:
                    if exc.existing_domain.status == Domain.Status.PROVISIONING:
                        existing, job, started = retry_domain_provisioning(exc.existing_domain)
                        if started:
                            _audit(
                                exc.existing_domain,
                                request,
                                "domain.provision_job_repaired",
                                existing,
                                {"job_id": str(job.id)},
                            )
                    messages.info(
                        request,
                        f"{exc.existing_domain.hostname} is already connected or being prepared.",
                    )
                else:
                    messages.warning(
                        request,
                        "This domain already has a different active routing mode. "
                        "The existing setup was kept unchanged.",
                    )
                return redirect("domain_detail", domain_id=exc.existing_domain.id)
            form.add_error(
                "hostname",
                "This domain is not available for a new ownership claim.",
            )
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            enqueue_job(
                kind="provision_domain",
                idempotency_key=f"provision-domain:{domain.id}",
                payload={"domain_id": str(domain.id)},
                domain=domain,
            )
            request.session["domain_id"] = str(domain.id)
            _audit(domain, request, "domain.claim_created", domain)
            request.session.pop(PENDING_DOMAIN_SESSION_KEY, None)
            messages.success(
                request,
                f"{domain.hostname} was added. We're preparing your DNS instructions.",
            )
            return redirect("domain_detail", domain_id=domain.id)
    return render(
        request,
        "inbox/domain_create.html",
        {"active_nav": "domains", "form": form},
    )


@verified_required
def domain_detail(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    request.session["domain_id"] = str(domain.id)
    domain = Domain.objects.prefetch_related("dns_records", "inbound_routes").get(id=domain.id)
    return render(
        request,
        "inbox/domain_detail.html",
        {
            "active_nav": "domains",
            "domain": domain,
            "can_retry_provisioning": can_retry_domain_provisioning(domain),
            "new_test_address": _active_domain_test_address(request, domain),
        },
    )


@verified_required
@require_POST
def domain_retry_provisioning(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    try:
        domain, job, started = retry_domain_provisioning(domain)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if started:
            _audit(
                domain,
                request,
                "domain.provision_retry_requested",
                domain,
                {"job_id": str(job.id)},
            )
            messages.success(
                request,
                "Setup retry started. Existing email settings will be inspected without "
                "overwriting them.",
            )
        else:
            messages.info(request, "A setup retry is already in progress.")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_create_test(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    try:
        test, address = create_domain_test(domain)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except Exception:
        logger.exception("Domain test route activation failed", extra={"domain_id": domain.id})
        messages.error(
            request,
            "The receiving route is still being activated. "
            "Try generating the test address again shortly.",
        )
    else:
        request.session[_domain_test_session_key(domain.id)] = {
            "address": address,
            "expires_at": test.expires_at.timestamp(),
        }
        _audit(domain, request, "domain.test_created", domain)
        messages.success(
            request,
            "Test address generated. Send a real email to it within 24 hours.",
        )
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_enable_outbound(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    try:
        domain, job, started = request_outbound_provisioning(domain)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if started:
            _audit(
                domain,
                request,
                "domain.outbound_provision_requested",
                domain,
                {"job_id": str(job.id)},
            )
            messages.success(
                request,
                "Sending setup started. We'll prepare the DKIM records for this domain.",
            )
        else:
            messages.info(request, "Sending setup is already in progress.")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_disable(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
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
    enqueue_job(
        kind="reconcile_receipt_rule",
        idempotency_key=f"receipt-rule:disable:{domain.id}",
        payload={},
        domain=domain,
    )
    _audit(domain, request, "domain.disabled", domain)
    messages.success(request, "The domain and its inbound routes were disabled.")
    return redirect("domains")


@verified_required
def reports_list(request: HttpRequest) -> HttpResponse:
    domain = current_domain(request)
    return render(
        request,
        "inbox/reports.html",
        {"active_nav": "reports", "reports": Report.objects.filter(domain=domain)},
    )


@verified_required
def notifications_list(request: HttpRequest) -> HttpResponse:
    domain = current_domain(request)
    if request.method == "POST":
        Notification.objects.filter(
            domain=domain,
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
                domain=domain, channel=Notification.Channel.IN_APP
            ),
        },
    )


@verified_required
def schedules_settings(request: HttpRequest) -> HttpResponse:
    domain = current_domain(request)
    schedule, _ = ReportSchedule.objects.get_or_create(domain=domain)
    retention, _ = RetentionPolicy.objects.get_or_create(domain=domain)
    schedule_form = ScheduleForm(
        request.POST or None,
        instance=schedule,
        domain=domain,
        prefix="schedule",
    )
    retention_form = RetentionForm(request.POST or None, instance=retention, prefix="retention")
    if request.method == "POST":
        if request.POST.get("form") == "schedule" and schedule_form.is_valid():
            schedule_form.save()
            domain.timezone = schedule_form.cleaned_data["timezone"]
            domain.save(update_fields=("timezone", "updated_at"))
            _audit(domain, request, "schedule.updated", schedule)
            messages.success(request, "Review schedule updated.")
            return redirect("schedules_settings")
        if request.POST.get("form") == "retention" and retention_form.is_valid():
            retention_form.save()
            _audit(domain, request, "retention.updated", retention)
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
    domain = current_domain(request)
    form = APITokenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token, raw = APIToken.issue(
            domain=domain,
            owner=request.user,
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
        )
        request.session["new_api_token"] = raw
        _audit(domain, request, "api_token.created", token, {"scopes": token.scopes})
        return redirect("api_tokens")
    new_token = request.session.pop("new_api_token", None)
    return render(
        request,
        "inbox/api_tokens.html",
        {
            "active_nav": "api_tokens",
            "form": form,
            "tokens": APIToken.objects.filter(domain=domain).order_by("-created_at"),
            "new_token": new_token,
        },
    )


@verified_required
@require_POST
def api_token_revoke(request: HttpRequest, token_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    token = domain_get_or_404(APIToken.objects, domain=domain, id=token_id)
    token.revoked_at = timezone.now()
    token.save(update_fields=("revoked_at", "updated_at"))
    _audit(domain, request, "api_token.revoked", token)
    messages.success(request, "API token revoked.")
    return redirect("api_tokens")


@verified_required
def audit_log(request: HttpRequest) -> HttpResponse:
    domain = current_domain(request)
    return render(
        request,
        "inbox/audit.html",
        {
            "active_nav": "audit",
            "audit_events": AuditEvent.objects.filter(domain=domain)[:250],
        },
    )


@verified_required
def attachment_download(request: HttpRequest, attachment_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    attachment = domain_get_or_404(Attachment.objects, domain=domain, id=attachment_id)
    try:
        authorized = authorized_attachment_url(attachment=attachment, domain=domain)
    except AttachmentGoneError as exc:
        return render(
            request, "inbox/attachment_unavailable.html", {"reason": str(exc)}, status=410
        )
    except AttachmentLockedError as exc:
        return render(
            request, "inbox/attachment_unavailable.html", {"reason": str(exc)}, status=423
        )
    _audit(domain, request, "attachment.download_authorized", attachment)
    return redirect(authorized.url)
