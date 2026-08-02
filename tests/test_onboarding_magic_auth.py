from __future__ import annotations

import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from sesame.utils import get_query_string, get_token

from inbox.models import Domain, ReportSchedule, RetentionPolicy, User

MAGIC_LINK_SCOPE = "operational-inbox-login"
LOCMEM_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def _workspace_for(user: User) -> Domain:
    domain = Domain.objects.create(
        owner=user,
        hostname=f"magic-{str(user.pk)[:8]}.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        claim_expires_at=timezone.now(),
    )
    ReportSchedule.objects.create(domain=domain)
    RetentionPolicy.objects.create(domain=domain)
    return domain


def _callback_for(user: User, *, scope: str = MAGIC_LINK_SCOPE, next_url: str = "") -> str:
    callback = reverse("sesame_login") + get_query_string(user, scope=scope)
    if next_url:
        callback += "&" + urlencode({"next": next_url})
    return callback


def _email_url(message) -> str:
    match = re.search(r"https?://[^\s<>]+", message.body)
    assert match is not None
    return match.group(0)


def _local_url(absolute_url: str) -> str:
    parsed = urlsplit(absolute_url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


@pytest.mark.django_db
def test_anonymous_home_renders_domain_first_landing_and_cta(client):
    response = client.get(reverse("home"))

    assert response.status_code == 200
    assert f'action="{reverse("start_onboarding")}"'.encode() in response.content
    assert b'name="hostname"' in response.content
    assert b'placeholder="your-domain.com"' in response.content
    assert b"Start managing mail" in response.content
    assert f'href="{reverse("login")}"'.encode() in response.content


@pytest.mark.django_db
def test_authenticated_home_redirects_to_dashboard(client, owner):
    client.force_login(owner)

    response = client.get(reverse("home"))

    assert response.status_code == 302
    assert response.url == reverse("dashboard")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("submitted", "normalized"),
    [
        ("  EXAMPLE.COM. ", "example.com"),
        ("BÜCHER.Example.", "xn--bcher-kva.example"),
    ],
)
def test_start_onboarding_normalizes_domain_into_session(client, submitted, normalized):
    response = client.post(reverse("start_onboarding"), {"hostname": submitted})

    assert response.status_code == 302
    assert response.url == reverse("signup")
    assert client.session["pending_domain"] == normalized


@pytest.mark.django_db
@pytest.mark.parametrize(
    "hostname",
    ["", "localhost", "owner@example.com", "*.example.com", "bad..example.com"],
)
def test_start_onboarding_rejects_invalid_domain_without_storing_it(client, hostname):
    response = client.post(reverse("start_onboarding"), {"hostname": hostname})

    assert response.status_code == 400
    assert "pending_domain" not in client.session
    assert b"domain" in response.content.lower()


@pytest.mark.django_db
def test_signup_collects_only_an_email_address(client):
    response = client.get(reverse("signup"))

    assert response.status_code == 200
    assert set(response.context["form"].fields) == {"email"}
    assert b'name="email"' in response.content
    assert b'name="organization_name"' not in response.content
    assert b'name="project_name"' not in response.content
    assert b'name="timezone"' not in response.content
    assert b'type="password"' not in response.content.lower()


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_magic_link_request_creates_passwordless_user_without_domain(client, mailoutbox):
    first = client.post(reverse("signup"), {"email": " New.Owner@EXAMPLE.com "})
    second = client.post(reverse("signup"), {"email": "new.owner@example.com"})

    assert first.status_code == 200
    assert second.status_code == 200
    user = User.objects.get(email="new.owner@example.com")
    assert user.is_active
    assert user.email_verified_at is None
    assert not user.has_usable_password()
    assert User.objects.filter(email=user.email).count() == 1
    assert not Domain.objects.filter(owner=user).exists()
    assert not ReportSchedule.objects.filter(domain__owner=user).exists()
    assert not RetentionPolicy.objects.filter(domain__owner=user).exists()
    assert len(mailoutbox) == 2


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_magic_link_request_keeps_existing_user_domainless_idempotently(
    client, mailoutbox
):
    user = User.objects.create_user(email="existing@example.com", password="Legacy-password-123")

    first = client.post(reverse("signup"), {"email": "existing@example.com"})
    second = client.post(reverse("signup"), {"email": "existing@example.com"})

    assert first.status_code == 200
    assert second.status_code == 200
    user.refresh_from_db()
    assert user.is_active
    assert not Domain.objects.filter(owner=user).exists()
    assert len(mailoutbox) == 2


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_magic_link_email_contains_the_sesame_callback_url(client, mailoutbox):
    response = client.post(reverse("signup"), {"email": "link@example.com"})

    assert response.status_code == 200
    assert response.context["sent"] is True
    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["link@example.com"]
    parsed = urlsplit(_email_url(mailoutbox[0]))
    public_base = urlsplit(settings.PUBLIC_BASE_URL)
    assert parsed.scheme == public_base.scheme
    assert parsed.netloc == public_base.netloc
    assert parsed.path == reverse("sesame_login")
    assert set(parse_qs(parsed.query)) >= {"sesame"}


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_valid_magic_link_logs_in_verifies_and_carries_domain_to_creation(client, mailoutbox):
    started = client.post(reverse("start_onboarding"), {"hostname": "MAIL.Example.COM."})
    assert started.status_code == 302
    requested = client.post(reverse("signup"), {"email": "domain-owner@example.com"})
    assert requested.status_code == 200

    user = User.objects.get(email="domain-owner@example.com")
    callback = client.get(_local_url(_email_url(mailoutbox[0])))

    assert callback.status_code == 302
    assert callback.url == reverse("domain_create")
    user.refresh_from_db()
    assert user.email_verified_at is not None
    assert client.session["_auth_user_id"] == str(user.pk)
    assert "domain_id" not in client.session
    domain_create = client.get(reverse("domain_create"))
    assert domain_create.status_code == 200
    assert domain_create.context["form"]["hostname"].value() == "mail.example.com"


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_signed_domain_handoff_survives_opening_magic_link_in_another_browser(client, mailoutbox):
    client.post(reverse("start_onboarding"), {"hostname": "other-device.example"})
    client.post(reverse("signup"), {"email": "other-device@example.com"})

    other_browser = Client()
    callback = other_browser.get(_local_url(_email_url(mailoutbox[0])))

    assert callback.status_code == 302
    assert callback.url == reverse("domain_create")
    assert other_browser.session["pending_domain"] == "other-device.example"
    domain_create = other_browser.get(reverse("domain_create"))
    assert domain_create.context["form"]["hostname"].value() == "other-device.example"


def _assert_invalid_link_redirect(response, client) -> None:
    assert response.status_code == 302
    assert response.url == f"{reverse('signup')}?auth_error=invalid-link"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_missing_magic_token_is_rejected(client):
    response = client.get(reverse("sesame_login"))

    _assert_invalid_link_redirect(response, client)


@pytest.mark.django_db
def test_tampered_magic_token_is_rejected(client):
    user = User.objects.create_user(email="tampered@example.com", password=None)
    token = get_token(user, scope=MAGIC_LINK_SCOPE)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    response = client.get(reverse("sesame_login") + "?" + urlencode({"sesame": tampered}))

    _assert_invalid_link_redirect(response, client)


@pytest.mark.django_db
def test_magic_token_with_wrong_scope_is_rejected(client):
    user = User.objects.create_user(email="scope@example.com", password=None)

    response = client.get(_callback_for(user, scope="some-other-purpose"))

    _assert_invalid_link_redirect(response, client)


@pytest.mark.django_db
def test_legacy_v1_shaped_magic_token_is_rejected_without_server_error(client):
    token = "AAAA:000000:" + ("A" * 27)

    response = client.get(reverse("sesame_login") + "?" + urlencode({"sesame": token}))

    _assert_invalid_link_redirect(response, client)


@pytest.mark.django_db
def test_expired_magic_token_is_rejected(client):
    user = User.objects.create_user(email="expired@example.com", password=None)
    with freeze_time("2026-08-01 11:49:00"):
        callback = _callback_for(user)

    with freeze_time("2026-08-01 12:00:00"):
        response = client.get(callback)

    _assert_invalid_link_redirect(response, client)


@pytest.mark.django_db
def test_magic_callback_accepts_safe_same_host_next_path(client):
    user = User.objects.create_user(email="safe-next@example.com", password=None)
    _workspace_for(user)

    response = client.get(_callback_for(user, next_url=reverse("inbox")))

    assert response.status_code == 302
    assert response.url == reverse("inbox")
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
def test_magic_callback_rejects_nested_magic_login_next_path(client):
    victim = User.objects.create_user(email="victim@example.com", password=None)
    attacker = User.objects.create_user(email="attacker@example.com", password=None)
    _workspace_for(attacker)
    attacker_callback = _callback_for(attacker)

    response = client.get(_callback_for(victim, next_url=attacker_callback))

    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session["_auth_user_id"] == str(victim.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("unsafe_next", ["https://evil.example/phish", "//evil.example/phish"])
def test_magic_callback_rejects_external_next_url(client, unsafe_next):
    user = User.objects.create_user(email=f"unsafe-{len(unsafe_next)}@example.com", password=None)
    _workspace_for(user)

    response = client.get(_callback_for(user, next_url=unsafe_next))

    assert response.status_code == 302
    assert response.url == reverse("dashboard")
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_magic_link_delivery_failure_is_honest_and_retryable(client, mailoutbox):
    with patch(
        "inbox.views.EmailMultiAlternatives.send",
        side_effect=RuntimeError("temporary SES failure"),
    ):
        response = client.post(reverse("signup"), {"email": "delivery@example.com"})

    assert response.status_code == 503
    assert response.context["sent"] is False
    assert response.context["form"].non_field_errors()
    assert b"Check your inbox" not in response.content
    assert User.objects.filter(email="delivery@example.com").exists()
    assert len(mailoutbox) == 0


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_legacy_inactive_unverified_user_can_complete_magic_login(client, mailoutbox):
    user = User.objects.create_user(
        email="legacy@example.com",
        password="Legacy-password-123",
        is_active=False,
    )
    assert user.email_verified_at is None

    requested = client.post(reverse("signup"), {"email": user.email})

    assert requested.status_code == 200
    user.refresh_from_db()
    assert user.is_active
    assert not user.has_usable_password()
    assert len(mailoutbox) == 1
    callback = client.get(_local_url(_email_url(mailoutbox[0])))
    assert callback.status_code == 302
    assert callback.url == reverse("dashboard")
    user.refresh_from_db()
    assert user.email_verified_at is not None
    assert client.session["_auth_user_id"] == str(user.pk)


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_inactive_already_verified_user_is_not_reactivated_or_issued_magic_link(client, mailoutbox):
    user = User.objects.create_user(
        email="disabled@example.com",
        password="Legacy-password-123",
        is_active=False,
        email_verified_at=timezone.now(),
    )

    response = client.post(reverse("signup"), {"email": user.email})

    assert response.status_code == 200
    assert response.context["sent"] is True
    user.refresh_from_db()
    assert not user.is_active
    assert user.has_usable_password()
    assert not Domain.objects.filter(owner=user).exists()
    assert len(mailoutbox) == 1
    assert reverse("sesame_login") not in mailoutbox[0].body


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND=LOCMEM_EMAIL_BACKEND)
def test_privileged_accounts_do_not_receive_public_magic_links(client, mailoutbox):
    user = User.objects.create_superuser(
        email="admin@example.com",
        password="Admin-password-123",
    )

    response = client.post(reverse("signup"), {"email": user.email})

    assert response.status_code == 200
    assert response.context["sent"] is True
    user.refresh_from_db()
    assert user.is_active
    assert user.has_usable_password()
    assert not Domain.objects.filter(owner=user).exists()
    assert len(mailoutbox) == 1
    assert reverse("sesame_login") not in mailoutbox[0].body


@pytest.mark.django_db
def test_magic_link_is_rejected_if_user_is_promoted_before_callback(client):
    user = User.objects.create_user(email="promoted@example.com", password=None)
    callback = _callback_for(user)
    user.is_staff = True
    user.save(update_fields=("is_staff",))

    response = client.get(callback)

    _assert_invalid_link_redirect(response, client)
    user.refresh_from_db()
    assert user.email_verified_at is None
    assert not Domain.objects.filter(owner=user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("route_name", ["home", "signup"])
def test_onboarding_and_auth_pages_do_not_render_password_fields(client, route_name):
    response = client.get(reverse(route_name))

    assert response.status_code == 200
    lowered = response.content.lower()
    assert b'type="password"' not in lowered
    assert b'name="password' not in lowered


@pytest.mark.django_db
def test_legacy_login_url_redirects_to_passwordless_signup_without_password_fields(client):
    response = client.get(reverse("login"), follow=True)

    assert response.redirect_chain == [(reverse("signup"), 302)]
    assert response.status_code == 200
    lowered = response.content.lower()
    assert b'type="password"' not in lowered
    assert b'name="password' not in lowered
