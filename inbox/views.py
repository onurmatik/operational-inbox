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
from django.db.models import Count, Exists, IntegerField, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from sesame.utils import get_parameters
from sesame.views import LoginView as SesameLoginView

from inbox.forms import (
    DomainForm,
    DraftRevisionForm,
    RetentionForm,
    SignupForm,
    StartOnboardingForm,
    VerificationResendForm,
)
from inbox.models import (
    APIToken,
    Attachment,
    AuditEvent,
    Conversation,
    ConversationTag,
    Domain,
    DomainDNSRecord,
    DomainTest,
    EmailVerificationToken,
    InboundRoute,
    InboundRoutingTransition,
    Message,
    MessageRecipient,
    OutboundMessage,
    ReplyDraft,
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
from inbox.services.billing import (
    billing_configured,
    construct_event,
    create_checkout_url,
    create_portal_url,
    price_summary,
    process_event,
)
from inbox.services.conversations import apply_conversation_action, mark_conversation_viewed
from inbox.services.domains import (
    DomainClaimConflict,
    DomainClaimLookupError,
    classify_stored_mx,
    create_domain,
    ensure_domain_test,
    inspect_domain_routing,
    normalize_hostname,
)
from inbox.services.drafts import (
    approve_exact_revision,
    resend_outbound,
    revise_draft,
)
from inbox.services.entitlements import can_manage_domain, for_user
from inbox.services.jobs import (
    can_retry_domain_provisioning,
    enqueue_job,
    request_outbound_provisioning,
    retry_domain_provisioning,
)
from inbox.services.outbound import (
    get_outbound_control,
    outbound_usage,
    set_outbound_paused,
)
from inbox.services.routing_transitions import (
    begin_routing_transition,
    cancel_routing_transition,
    ensure_routing_transition_test,
)
from inbox.services.tags import add_conversation_tag, normalize_tag, remove_conversation_tag
from inbox.services.tenancy import current_domain, domain_get_or_404, get_owned_domain

logger = logging.getLogger(__name__)

PENDING_DOMAIN_SESSION_KEY = "pending_domain"
MAGIC_LINK_SCOPE = "operational-inbox-login"
MAGIC_LINK_MAX_AGE_SECONDS = 10 * 60
ONBOARDING_STATE_SALT = "operational-inbox-onboarding-v1"


ACTIVE_ROUTING_TRANSITION_STATUSES = (
    InboundRoutingTransition.Status.PREPARING,
    InboundRoutingTransition.Status.WAITING_DNS,
    InboundRoutingTransition.Status.WAITING_TEST,
    InboundRoutingTransition.Status.GRACE,
    InboundRoutingTransition.Status.FAILED,
)

RECENT_AUDIT_EVENT_LIMIT = 6
AUDIT_PROBLEM_EVENT_TYPES = frozenset(
    {
        "agent.draft_failed",
        "agent.report_failed",
        "domain.outbound_provision_failed",
        "domain.provision_failed",
    }
)
AUDIT_PROBLEM_STATUSES = frozenset({"BOUNCED", "COMPLAINED", "FAILED", "UNKNOWN"})


def _active_routing_transition(domain: Domain) -> InboundRoutingTransition | None:
    transitions = sorted(
        (
            transition
            for transition in domain.routing_transitions.all()
            if transition.status in ACTIVE_ROUTING_TRANSITION_STATUSES
        ),
        key=lambda transition: (transition.created_at, str(transition.id)),
        reverse=True,
    )
    return transitions[0] if transitions else None


def _domain_readiness_alert(domain: Domain) -> dict[str, str] | None:
    inbound_problem = not domain.inbound_ready
    outbound_problem = (
        domain.outbound_status != Domain.OutboundStatus.DISABLED and not domain.outbound_ready
    )
    if not inbound_problem and not outbound_problem:
        return None

    danger = domain.status in {Domain.Status.ERROR, Domain.Status.DEGRADED} or (
        domain.outbound_status in {Domain.OutboundStatus.ERROR, Domain.OutboundStatus.DEGRADED}
    )
    if domain.error_code:
        message = domain.public_error_message
    elif domain.outbound_error_code:
        message = domain.public_outbound_error_message
    elif inbound_problem:
        message = f"Receiving is not ready. Current domain status: {domain.get_status_display()}."
    else:
        message = (
            f"Sending is not ready. Current sending status: {domain.get_outbound_status_display()}."
        )
    return {"severity": "danger" if danger else "warning", "message": message}


def _audit_event_has_problem(event: AuditEvent) -> bool:
    status = str(event.metadata.get("status", "")).upper()
    return event.event_type in AUDIT_PROBLEM_EVENT_TYPES or status in AUDIT_PROBLEM_STATUSES


def _active_domain_test(
    domain: Domain,
    *,
    active_transition: InboundRoutingTransition | None = None,
) -> DomainTest | None:
    tests = domain.tests.filter(
        status=DomainTest.Status.PENDING,
        expires_at__gt=timezone.now(),
        address__isnull=False,
        received_message__isnull=True,
    ).exclude(address="")
    if active_transition is not None:
        if active_transition.status != InboundRoutingTransition.Status.WAITING_TEST:
            return None
        expected_route_kind = (
            InboundRoute.Kind.DIRECT_DOMAIN
            if active_transition.to_mode == Domain.SetupMode.DIRECT_MX
            else InboundRoute.Kind.FORWARDING_ALIAS
        )
        return (
            tests.filter(
                routing_transition=active_transition,
                setup_generation=active_transition.generation,
                expected_setup_mode=active_transition.to_mode,
                expected_route_kind=expected_route_kind,
            )
            .order_by("-created_at", "-id")
            .first()
        )
    if domain.status != Domain.Status.PENDING_TEST:
        return None
    expected_route_kind = (
        InboundRoute.Kind.DIRECT_DOMAIN
        if domain.setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )
    return (
        tests.filter(
            routing_transition__isnull=True,
            setup_generation=domain.inbound_setup_generation,
            expected_setup_mode=domain.setup_mode,
            expected_route_kind=expected_route_kind,
        )
        .order_by("-created_at", "-id")
        .first()
    )


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


def _domain_required_redirect(request: HttpRequest, feature: str) -> HttpResponse:
    messages.info(request, f"Connect a domain to use {feature}.")
    return redirect("domains")


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


def _upgrade_required(request: HttpRequest, feature: str) -> HttpResponse:
    messages.warning(request, f"{feature} requires Operational Inbox Pro.")
    return redirect("billing")


def _domain_write_required(request: HttpRequest, domain: Domain) -> HttpResponse | None:
    if can_manage_domain(request.user, domain):
        return None
    return _upgrade_required(request, "Managing additional domains")


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
            user.domains.exclude(status=Domain.Status.DISABLED).order_by("created_at").first()
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
    domains = list(request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname"))
    if not domains:
        return redirect("domain_create")
    domain_ids = [domain.id for domain in domains]
    messages_qs = Message.objects.filter(domain_id__in=domain_ids)
    recent_messages = (
        messages_qs.filter(
            direction=Message.Direction.INBOUND,
            conversation__trashed_at__isnull=True,
        )
        .select_related("conversation", "domain")
        .prefetch_related("conversation__tags", "recipients")
        .order_by("-received_at")[:8]
    )
    recent_audit_events = list(
        AuditEvent.objects.filter(domain_id__in=domain_ids).select_related("domain")[
            :RECENT_AUDIT_EVENT_LIMIT
        ]
    )
    domain_readiness_alerts = []
    for domain in domains:
        if alert := _domain_readiness_alert(domain):
            domain_readiness_alerts.append({"domain": domain, **alert})
    context = {
        "active_nav": "overview",
        "recent_messages": recent_messages,
        "domain_readiness_alerts": domain_readiness_alerts,
        "audit_alert_events": [
            event for event in recent_audit_events if _audit_event_has_problem(event)
        ],
        "metrics": {
            "new_messages": messages_qs.filter(
                direction=Message.Direction.INBOUND,
                viewed_at__isnull=True,
                conversation__archived_at__isnull=True,
                conversation__trashed_at__isnull=True,
            ).count(),
            "quarantined": messages_qs.filter(is_quarantined=True).count(),
            "mailboxes": MessageRecipient.objects.filter(
                domain_id__in=domain_ids,
                is_routing_recipient=True,
            )
            .values("address")
            .distinct()
            .count(),
        },
    }
    return render(request, "inbox/dashboard.html", context)


@verified_required
def inbox_list(request: HttpRequest) -> HttpResponse:
    requested_domain = request.GET.get("domain", "").strip()
    if requested_domain:
        domain = get_owned_domain(request.user, requested_domain)
        if request.session.get("domain_id") != str(domain.id):
            request.session["domain_id"] = str(domain.id)
    else:
        domain = current_domain(request)

    folders = {
        "inbox": "Inbox",
        "starred": "Starred",
        "archive": "Archive",
        "trash": "Trash",
    }
    folder = request.GET.get("folder", "inbox")
    if folder not in folders:
        folder = "inbox"

    message_totals = (
        Message.objects.filter(conversation=OuterRef("pk"))
        .order_by()
        .values("conversation")
        .annotate(total=Count("id"))
        .values("total")[:1]
    )
    new_message_totals = (
        Message.objects.filter(
            conversation=OuterRef("pk"),
            direction=Message.Direction.INBOUND,
            viewed_at__isnull=True,
        )
        .order_by()
        .values("conversation")
        .annotate(total=Count("id"))
        .values("total")[:1]
    )
    latest_message = Message.objects.filter(conversation=OuterRef("pk")).order_by(
        "-received_at", "-created_at"
    )
    conversations = Conversation.objects.filter(domain=domain).annotate(
        message_count=Coalesce(
            Subquery(message_totals, output_field=IntegerField()),
            Value(0),
        ),
        new_message_count=Coalesce(
            Subquery(new_message_totals, output_field=IntegerField()),
            Value(0),
        ),
        preview_from_address=Subquery(latest_message.values("from_address")[:1]),
        preview_text_body=Subquery(latest_message.values("text_body")[:1]),
        preview_is_quarantined=Subquery(latest_message.values("is_quarantined")[:1]),
        has_quarantined=Exists(
            Message.objects.filter(conversation=OuterRef("pk"), is_quarantined=True)
        ),
    )
    if folder == "inbox":
        conversations = conversations.filter(archived_at__isnull=True, trashed_at__isnull=True)
    elif folder == "starred":
        conversations = conversations.filter(starred_at__isnull=False, trashed_at__isnull=True)
    elif folder == "archive":
        conversations = conversations.filter(archived_at__isnull=False, trashed_at__isnull=True)
    else:
        conversations = conversations.filter(trashed_at__isnull=False)

    recipient = request.GET.get("recipient", "").strip().casefold()
    if recipient:
        if not MessageRecipient.objects.filter(
            domain=domain,
            is_routing_recipient=True,
            address__iexact=recipient,
        ).exists():
            raise Http404
        conversations = conversations.filter(
            messages__direction=Message.Direction.INBOUND,
            messages__recipients__is_routing_recipient=True,
            messages__recipients__address__iexact=recipient,
        ).distinct()
    query = request.GET.get("q", "").strip()
    if query:
        conversations = conversations.filter(
            Q(subject__icontains=query)
            | Q(messages__from_address__icontains=query)
            | Q(messages__text_body__icontains=query)
        ).distinct()
    raw_tag = request.GET.get("tag", "")
    tag = ""
    if raw_tag:
        try:
            _, tag = normalize_tag(raw_tag)
        except ValidationError:
            tag = raw_tag.casefold()[:64]
        conversations = conversations.filter(tags__normalized_name=tag).distinct()
    security = request.GET.get("security", "")
    if security == "suspicious":
        conversations = conversations.filter(messages__is_suspicious=True).distinct()
    elif security == "quarantined":
        conversations = conversations.filter(messages__is_quarantined=True).distinct()
    ordered_conversations = conversations.prefetch_related(
        Prefetch("tags", queryset=ConversationTag.objects.order_by("normalized_name"))
    ).order_by("-last_message_at")
    paginator = Paginator(ordered_conversations, 50)
    page = paginator.get_page(request.GET.get("page"))
    conversation_ids = [conversation.id for conversation in page.object_list]
    routing_addresses: dict[uuid.UUID, list[dict[str, str]]] = {}
    if conversation_ids:
        recipient_rows = (
            MessageRecipient.objects.filter(
                domain=domain,
                message__conversation_id__in=conversation_ids,
                message__direction=Message.Direction.INBOUND,
                is_routing_recipient=True,
            )
            .values("message__conversation_id", "address")
            .order_by("message__conversation_id", "address")
            .distinct()
        )
        for row in recipient_rows:
            address = row["address"]
            routing_addresses.setdefault(row["message__conversation_id"], []).append(
                {
                    "address": address,
                    "local_part": address.rsplit("@", 1)[0],
                }
            )
    for conversation in page.object_list:
        conversation.routing_addresses = routing_addresses.get(conversation.id, [])

    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    clear_params = {"domain": str(domain.id)}
    if folder != "inbox":
        clear_params["folder"] = folder
    return render(
        request,
        "inbox/inbox_list.html",
        {
            "active_nav": "inbox",
            "domain": domain,
            "folder": folder,
            "folder_label": folders[folder],
            "conversations": page.object_list,
            "page_obj": page,
            "pagination_query": pagination_params.urlencode(),
            "filters": {
                "q": query,
                "tag": tag,
                "security": security,
                "recipient": recipient,
            },
            "observed_tags": ConversationTag.objects.filter(domain=domain)
            .values_list("name", flat=True)
            .order_by("normalized_name")
            .distinct(),
            "has_active_filters": any((query, tag, security, recipient)),
            "clear_filters_url": f"{reverse('inbox')}?{urlencode(clear_params)}",
        },
    )


OUTBOUND_PROBLEM_STATUSES = {
    OutboundMessage.Status.FAILED,
    OutboundMessage.Status.UNKNOWN,
    OutboundMessage.Status.BOUNCED,
    OutboundMessage.Status.COMPLAINED,
}


@verified_required
def outbox(request: HttpRequest) -> HttpResponse:
    domains = request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname")
    outbound = (
        OutboundMessage.objects.filter(domain__in=domains)
        .select_related("domain", "conversation")
        .prefetch_related("delivery_events")
    )
    selected_domain = request.GET.get("domain", "").strip()
    selected_status = request.GET.get("status", "").strip().upper()
    recipient = request.GET.get("recipient", "").strip()
    selected_range = request.GET.get("time", "24h")
    if selected_domain:
        try:
            selected = domains.get(id=selected_domain)
        except (Domain.DoesNotExist, ValidationError, ValueError) as exc:
            raise Http404 from exc
        outbound = outbound.filter(domain=selected)
    if selected_status in OutboundMessage.Status.values:
        outbound = outbound.filter(status=selected_status)
    else:
        selected_status = ""
    if recipient:
        outbound = outbound.filter(to_address__icontains=recipient)
    windows = {
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
    }
    if selected_range not in {*windows, "all"}:
        selected_range = "24h"
    if selected_range != "all":
        outbound = outbound.filter(created_at__gte=timezone.now() - windows[selected_range])
    page = Paginator(outbound.order_by("-created_at"), 50).get_page(request.GET.get("page"))
    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    recent = OutboundMessage.objects.filter(
        domain__in=domains, created_at__gte=timezone.now() - timedelta(hours=24)
    )
    control = get_outbound_control(request.user)
    usage = outbound_usage(request.user)
    domain_usage = [
        {
            "domain": domain,
            "count": usage["by_domain"].get(str(domain.id), 0),
        }
        for domain in domains
    ]
    return render(
        request,
        "inbox/outbox.html",
        {
            "active_nav": "outbox",
            "domains": domains,
            "page": page,
            "statuses": OutboundMessage.Status.choices,
            "selected_domain": selected_domain,
            "selected_status": selected_status,
            "recipient": recipient,
            "selected_range": selected_range,
            "pagination_query": pagination_params.urlencode(),
            "control": control,
            "usage": usage,
            "domain_usage": domain_usage,
            "metrics": {
                "queued": recent.filter(status=OutboundMessage.Status.QUEUED).count(),
                "in_flight": recent.filter(
                    status__in={
                        OutboundMessage.Status.SUBMITTING,
                        OutboundMessage.Status.ACCEPTED,
                    }
                ).count(),
                "delivered": recent.filter(status=OutboundMessage.Status.DELIVERED).count(),
                "problems": recent.filter(status__in=OUTBOUND_PROBLEM_STATUSES).count(),
            },
        },
    )


@verified_required
@require_POST
def outbox_control(request: HttpRequest) -> HttpResponse:
    if not for_user(request.user).outbound:
        return _upgrade_required(request, "Outbound sending")
    paused = request.POST.get("paused") == "true"
    control = set_outbound_paused(request.user, paused=paused)
    for domain in request.user.domains.exclude(status=Domain.Status.DISABLED):
        _audit(
            domain,
            request,
            "outbound.paused" if control.is_paused else "outbound.resumed",
            domain,
        )
    messages.success(
        request,
        "External sending is paused." if control.is_paused else "External sending resumed.",
    )
    return redirect("outbox")


@verified_required
def conversation_detail(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    requested_domain = request.GET.get("domain", "").strip()
    if requested_domain:
        domain = get_owned_domain(request.user, requested_domain)
        if request.session.get("domain_id") != str(domain.id):
            request.session["domain_id"] = str(domain.id)
    else:
        domain = current_domain(request)
    with transaction.atomic():
        conversation = domain_get_or_404(
            Conversation.objects.select_for_update().prefetch_related("tags"),
            domain=domain,
            id=conversation_id,
        )
        viewed_messages = mark_conversation_viewed(conversation)
        if viewed_messages:
            _audit(
                domain,
                request,
                "conversation.viewed",
                conversation,
                {"messages_viewed": viewed_messages},
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
            "outbound_messages": conversation.outbound_messages.prefetch_related(
                "delivery_events"
            ).order_by("-created_at"),
            "has_quarantined": conversation.messages.filter(is_quarantined=True).exists(),
        },
    )


@verified_required
@require_POST
def conversation_action(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    next_url = _safe_next(request, request.POST.get("next") or reverse("inbox"))
    if denied := _domain_write_required(request, domain):
        return denied
    action = request.POST.get("action", "")
    event_types = {
        "star": "conversation.starred",
        "unstar": "conversation.unstarred",
        "archive": "conversation.archived",
        "trash": "conversation.trashed",
        "restore": "conversation.restored",
    }
    feedback = {
        "star": "Conversation starred.",
        "unstar": "Conversation unstarred.",
        "archive": "Conversation moved to Archive.",
        "trash": "Conversation moved to Trash.",
        "restore": "Conversation restored to Inbox.",
    }
    try:
        with transaction.atomic():
            conversation = domain_get_or_404(
                Conversation.objects.select_for_update(),
                domain=domain,
                id=conversation_id,
            )
            result = apply_conversation_action(conversation, action)
            if result.state_changed:
                _audit(
                    domain,
                    request,
                    event_types[action],
                    conversation,
                    {"action": action, "messages_viewed": result.viewed_messages},
                )
    except (KeyError, ValidationError) as exc:
        error = (
            exc.messages[0] if isinstance(exc, ValidationError) else "Select a supported action."
        )
        messages.error(request, error)
        return redirect(next_url)
    if result.changed:
        messages.success(request, feedback[action])
    return redirect(next_url)


@verified_required
@require_POST
def conversation_tag(request: HttpRequest, conversation_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    next_url = _safe_next(
        request,
        request.POST.get("next") or reverse("conversation_detail", args=[conversation_id]),
    )
    if denied := _domain_write_required(request, domain):
        return denied
    operation = request.POST.get("operation", "add")
    value = request.POST.get("tag", "")
    try:
        with transaction.atomic():
            conversation = domain_get_or_404(
                Conversation.objects.select_for_update(),
                domain=domain,
                id=conversation_id,
            )
            if operation == "add":
                tag, changed = add_conversation_tag(conversation, value)
                event_type = "conversation.tag_added"
                feedback = f"Tag #{tag.name} added."
            elif operation == "remove":
                tag = remove_conversation_tag(conversation, value)
                changed = tag is not None
                event_type = "conversation.tag_removed"
                feedback = f"Tag #{tag.name} removed." if tag else ""
            else:
                raise ValidationError("Select a supported tag action.")
            if changed and tag is not None:
                _audit(
                    domain,
                    request,
                    event_type,
                    tag,
                    {"conversation_id": str(conversation.id), "tag": tag.normalized_name},
                )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(next_url)
    if changed:
        messages.success(request, feedback)
    return redirect(next_url)


@verified_required
@require_POST
def draft_revise(request: HttpRequest, draft_id: uuid.UUID) -> HttpResponse:
    domain = current_domain(request)
    if not for_user(request.user).outbound:
        return _upgrade_required(request, "Reply drafts")
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
    if not for_user(request.user).outbound:
        return _upgrade_required(request, "Outbound sending")
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
    if not for_user(request.user).outbound:
        return _upgrade_required(request, "Outbound sending")
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
            "limit": for_user(request.user).domain_limit,
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
    cache_key = f"domain-routing-inspection:v3:{cache_digest}"
    payload = cache.get(cache_key)
    if payload is None:
        try:
            inspection = inspect_domain_routing(hostname)
        except DomainClaimLookupError as exc:
            return JsonResponse(
                {
                    "code": "claim_lookup_failed",
                    "message": "; ".join(exc.messages),
                },
                status=503,
            )
        except ValidationError as exc:
            return JsonResponse(
                {
                    "code": "mx_lookup_failed",
                    "message": "; ".join(exc.messages),
                },
                status=503,
            )
        serialized_records = [
            {"preference": record.preference, "exchange": record.exchange}
            for record in inspection.mx_records
        ]
        payload = {
            "hostname": hostname,
            "has_existing_mx": bool(serialized_records),
            "mx_classification": inspection.classification.value,
            "has_operational_inbox_claim": inspection.has_operational_inbox_claim,
            "recommended_setup_mode": inspection.recommended_setup_mode,
            "requires_explicit_choice": inspection.requires_explicit_choice,
            "mx_records": serialized_records,
        }
        cache.set(cache_key, payload, timeout=60)

    return JsonResponse(payload)


@verified_required
def domain_create_view(request: HttpRequest) -> HttpResponse:
    pending_domain = request.session.get(PENDING_DOMAIN_SESSION_KEY, "")
    form = DomainForm(
        request.POST or None,
        initial={"hostname": pending_domain} if pending_domain else None,
    )
    entitlements = for_user(request.user)
    active_domain_count = request.user.domains.exclude(status=Domain.Status.DISABLED).count()
    if active_domain_count >= entitlements.domain_limit:
        return _upgrade_required(request, "Connecting another domain")
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
                payload={
                    "domain_id": str(domain.id),
                    "setup_generation": domain.inbound_setup_generation,
                    "setup_mode": domain.setup_mode,
                },
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
    domain = Domain.objects.prefetch_related(
        "dns_records",
        "inbound_routes",
        "routing_transitions__routes",
    ).get(id=domain.id)
    active_transition = _active_routing_transition(domain)
    routes = list(domain.inbound_routes.all())
    active_route_kind = (
        InboundRoute.Kind.DIRECT_DOMAIN
        if domain.setup_mode == Domain.SetupMode.DIRECT_MX
        else InboundRoute.Kind.FORWARDING_ALIAS
    )
    active_route = next(
        iter(
            sorted(
                (route for route in routes if route.is_active and route.kind == active_route_kind),
                key=lambda route: (route.setup_generation, route.created_at, str(route.id)),
                reverse=True,
            )
        ),
        None,
    )
    source_route = None
    target_route = None
    if active_transition is not None:
        source_route_kind = (
            InboundRoute.Kind.DIRECT_DOMAIN
            if active_transition.from_mode == Domain.SetupMode.DIRECT_MX
            else InboundRoute.Kind.FORWARDING_ALIAS
        )
        source_routes = (
            route
            for route in routes
            if route.is_active
            and route.kind == source_route_kind
            and route.routing_transition_id != active_transition.id
            and (
                (
                    active_transition.status == InboundRoutingTransition.Status.GRACE
                    and route.grace_until == active_transition.grace_until
                )
                or (
                    active_transition.status != InboundRoutingTransition.Status.GRACE
                    and route.setup_generation == domain.inbound_setup_generation
                )
            )
        )
        source_route = next(
            iter(
                sorted(
                    source_routes,
                    key=lambda route: (route.setup_generation, route.created_at, str(route.id)),
                    reverse=True,
                )
            ),
            None,
        )
        target_route_kind = (
            InboundRoute.Kind.DIRECT_DOMAIN
            if active_transition.to_mode == Domain.SetupMode.DIRECT_MX
            else InboundRoute.Kind.FORWARDING_ALIAS
        )
        target_route = next(
            iter(
                sorted(
                    (
                        route
                        for route in routes
                        if route.routing_transition_id == active_transition.id
                        and route.kind == target_route_kind
                    ),
                    key=lambda route: (route.setup_generation, route.created_at, str(route.id)),
                    reverse=True,
                )
            ),
            None,
        )
    existing_mx_layout = classify_stored_mx(domain.existing_mx)
    dns_records = list(domain.dns_records.all())
    display_dns_records = dns_records
    if active_transition is not None:
        target_purposes = {DomainDNSRecord.Purpose.OWNERSHIP}
        if active_transition.to_mode == Domain.SetupMode.DIRECT_MX:
            target_purposes.update(
                {
                    DomainDNSRecord.Purpose.SES_VERIFICATION,
                    DomainDNSRecord.Purpose.MX,
                }
            )
        display_dns_records = [
            record for record in dns_records if record.purpose in target_purposes
        ]
    elif domain.setup_mode == Domain.SetupMode.PROVIDER_FORWARD:
        visible_purposes = {DomainDNSRecord.Purpose.OWNERSHIP}
        if domain.outbound_status != Domain.OutboundStatus.DISABLED:
            visible_purposes.update(
                {
                    DomainDNSRecord.Purpose.SES_VERIFICATION,
                    DomainDNSRecord.Purpose.DKIM,
                    DomainDNSRecord.Purpose.SPF,
                    DomainDNSRecord.Purpose.DMARC,
                }
            )
        display_dns_records = [
            record for record in dns_records if record.purpose in visible_purposes
        ]
    active_domain_test = _active_domain_test(domain, active_transition=active_transition)
    test_scope_waiting = (
        active_transition is not None
        and active_transition.status == InboundRoutingTransition.Status.WAITING_TEST
    ) or (active_transition is None and domain.status == Domain.Status.PENDING_TEST)
    return render(
        request,
        "inbox/domain_detail.html",
        {
            "active_nav": "domains",
            "domain": domain,
            "can_retry_provisioning": can_retry_domain_provisioning(domain),
            "existing_mx_layout": existing_mx_layout.value,
            "active_transition": active_transition,
            "active_route": active_route,
            "source_route": source_route,
            "target_route": target_route,
            "display_dns_records": display_dns_records,
            "transition_can_cancel": (
                active_transition is not None
                and active_transition.status != InboundRoutingTransition.Status.GRACE
            ),
            "new_test_address": active_domain_test.address if active_domain_test else None,
            "test_address_unavailable": test_scope_waiting and active_domain_test is None,
        },
    )


@verified_required
@require_POST
def domain_retry_provisioning(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    if denied := _domain_write_required(request, domain):
        return denied
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


def _start_domain_routing_transition(
    request: HttpRequest,
    domain_id: uuid.UUID,
    target_mode: str,
) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    if denied := _domain_write_required(request, domain):
        return denied
    try:
        transition, started = begin_routing_transition(domain, target_mode)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if started:
            job = enqueue_job(
                kind="provision_routing_transition",
                idempotency_key=(
                    f"provision-routing-transition:{transition.id}:{transition.generation}"
                ),
                payload={
                    "transition_id": str(transition.id),
                    "generation": transition.generation,
                },
                domain=domain,
            )
            _audit(
                domain,
                request,
                "domain.routing_transition_started",
                transition,
                {
                    "from": transition.from_mode,
                    "to": transition.to_mode,
                    "generation": transition.generation,
                    "job_id": str(job.id),
                },
            )
            messages.success(
                request,
                "Receiving route change started. Your current route remains active while the "
                "target route is prepared and tested.",
            )
        else:
            messages.info(request, "This receiving route change is already in progress.")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_routing_transition_start(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    return _start_domain_routing_transition(
        request,
        domain_id,
        request.POST.get("target_mode", ""),
    )


@verified_required
@require_POST
def domain_switch_to_direct(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    """Backward-compatible entry point for the former direct-only route switch."""

    return _start_domain_routing_transition(
        request,
        domain_id,
        Domain.SetupMode.DIRECT_MX,
    )


@verified_required
@require_POST
def domain_routing_transition_cancel(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    if denied := _domain_write_required(request, domain):
        return denied
    transition = (
        domain.routing_transitions.filter(status__in=ACTIVE_ROUTING_TRANSITION_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )
    if transition is None:
        messages.info(request, "There is no active receiving route change to cancel.")
        return redirect("domain_detail", domain_id=domain.id)
    try:
        cancelled = cancel_routing_transition(transition)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if cancelled:
            _audit(
                domain,
                request,
                "domain.routing_transition_cancelled",
                transition,
                {
                    "from": transition.from_mode,
                    "to": transition.to_mode,
                    "generation": transition.generation,
                },
            )
            if transition.from_mode == Domain.SetupMode.PROVIDER_FORWARD:
                cancellation_message = (
                    "Receiving route change cancelled. Operational Inbox kept provider "
                    "forwarding as the configured route. External DNS was not changed. If you "
                    "changed MX records, restore your mail provider's MX records in DNS now."
                )
            else:
                cancellation_message = (
                    "Receiving route change cancelled. Operational Inbox kept direct MX routing "
                    "as the configured route. External DNS was not changed. If you changed MX "
                    "records, restore the single Operational Inbox MX record in DNS now."
                )
            messages.success(request, cancellation_message)
        else:
            messages.info(request, "This receiving route change is no longer cancellable.")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_create_test(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    if denied := _domain_write_required(request, domain):
        return denied
    active_transition = (
        domain.routing_transitions.filter(status__in=ACTIVE_ROUTING_TRANSITION_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )
    try:
        if active_transition is not None:
            if active_transition.status != InboundRoutingTransition.Status.WAITING_TEST:
                raise ValidationError(
                    "Verify the target receiving route before preparing its test address."
                )
            test, address, created = ensure_routing_transition_test(active_transition)
        else:
            test, address, created = ensure_domain_test(domain)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except Exception:
        logger.exception("Domain test route activation failed", extra={"domain_id": domain.id})
        messages.error(
            request,
            "The receiving route is still being activated. "
            "Try preparing the test address again shortly.",
        )
    else:
        if created:
            if test.routing_transition_id is not None:
                _audit(domain, request, "domain.routing_transition_test_created", test)
            else:
                _audit(domain, request, "domain.test_created", domain)
            messages.success(
                request,
                "Target-path test address is ready. Send a new email to it within 24 hours."
                if test.routing_transition_id is not None
                else "Test address is ready. Send a new email to it within 24 hours.",
            )
        else:
            messages.info(request, f"The active test address is ready: {address}")
    return redirect("domain_detail", domain_id=domain.id)


@verified_required
@require_POST
def domain_enable_outbound(request: HttpRequest, domain_id: uuid.UUID) -> HttpResponse:
    domain = get_owned_domain(request.user, domain_id)
    if not for_user(request.user).outbound:
        return _upgrade_required(request, "Outbound sending")
    if denied := _domain_write_required(request, domain):
        return denied
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
    if denied := _domain_write_required(request, domain):
        return denied
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
    domain.routing_transitions.filter(status__in=ACTIVE_ROUTING_TRANSITION_STATUSES).update(
        status=InboundRoutingTransition.Status.CANCELLED,
        cancelled_at=now,
        updated_at=now,
    )
    domain.tests.filter(
        status=DomainTest.Status.PENDING,
    ).update(status=DomainTest.Status.EXPIRED, updated_at=now)
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
def retention_settings(request: HttpRequest) -> HttpResponse:
    try:
        domain = current_domain(request)
    except Http404:
        return _domain_required_redirect(request, "retention settings")
    retention, _ = RetentionPolicy.objects.get_or_create(domain=domain)
    retention_form = RetentionForm(request.POST or None, instance=retention, prefix="retention")
    if request.method == "POST" and not for_user(request.user).custom_settings:
        return _upgrade_required(request, "Custom retention")
    if request.method == "POST" and retention_form.is_valid():
        retention_form.save()
        _audit(domain, request, "retention.updated", retention)
        messages.success(request, "Retention policy updated.")
        return redirect("retention_settings")
    return render(
        request,
        "inbox/settings_retention.html",
        {
            "active_nav": "retention",
            "retention_form": retention_form,
        },
    )


@verified_required
def api_tokens(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        had_token = APIToken.objects.filter(
            owner=request.user,
            revoked_at__isnull=True,
        ).exists()
        _, raw = APIToken.issue(owner=request.user)
        request.session["new_api_token"] = raw
        messages.success(
            request,
            "Personal API token regenerated." if had_token else "Personal API token created.",
        )
        return redirect("api_tokens")
    new_token = request.session.pop("new_api_token", None)
    return render(
        request,
        "inbox/api_tokens.html",
        {
            "active_nav": "api_tokens",
            "token": APIToken.objects.filter(owner=request.user).order_by("-created_at").first(),
            "new_token": new_token,
        },
    )


@verified_required
@require_POST
def api_token_revoke(request: HttpRequest, token_id: uuid.UUID) -> HttpResponse:
    token = APIToken.objects.filter(
        owner=request.user,
        id=token_id,
        revoked_at__isnull=True,
    ).first()
    if token is None:
        raise Http404
    token.revoked_at = timezone.now()
    token.save(update_fields=("revoked_at", "updated_at"))
    messages.success(request, "Personal API token revoked.")
    return redirect("api_tokens")


@verified_required
def audit_log(request: HttpRequest) -> HttpResponse:
    try:
        domain = current_domain(request)
    except Http404:
        return _domain_required_redirect(request, "audit history")
    return render(
        request,
        "inbox/audit.html",
        {
            "active_nav": "audit",
            "audit_events": AuditEvent.objects.filter(domain=domain)[:250],
        },
    )


@verified_required
def billing(request: HttpRequest) -> HttpResponse:
    profile = getattr(request.user, "billing_profile", None)
    price = None
    if billing_configured():
        try:
            price = price_summary()
        except Exception:
            logger.exception("Stripe price lookup failed")
    return render(
        request,
        "inbox/billing.html",
        {
            "active_nav": "billing",
            "billing_profile": profile,
            "billing_configured": billing_configured(),
            "price": price,
            "pro_domain_limit": settings.MAX_DOMAINS_PER_USER,
        },
    )


@verified_required
@require_POST
def billing_checkout(request: HttpRequest) -> HttpResponse:
    try:
        url = create_checkout_url(request.user)
    except ValidationError as exc:
        messages.info(request, "; ".join(exc.messages))
        return redirect("billing")
    except Exception:
        logger.exception("Stripe Checkout creation failed", extra={"user_id": request.user.id})
        messages.error(request, "Checkout is temporarily unavailable. Try again shortly.")
        return redirect("billing")
    return redirect(url)


@verified_required
@require_POST
def billing_portal(request: HttpRequest) -> HttpResponse:
    try:
        url = create_portal_url(request.user)
    except Exception:
        logger.exception(
            "Stripe customer portal creation failed", extra={"user_id": request.user.id}
        )
        messages.error(request, "Subscription management is temporarily unavailable.")
        return redirect("billing")
    return redirect(url)


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> JsonResponse:
    try:
        event = construct_event(request.body, request.headers.get("Stripe-Signature", ""))
    except Exception as exc:
        # Stripe signature errors intentionally receive the same non-sensitive response.
        logger.warning("Stripe webhook rejected", extra={"error_type": type(exc).__name__})
        return JsonResponse({"received": False}, status=400)
    try:
        process_event(event)
    except Exception:
        logger.exception("Stripe webhook processing failed")
        return JsonResponse({"received": False}, status=500)
    return JsonResponse({"received": True})


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
