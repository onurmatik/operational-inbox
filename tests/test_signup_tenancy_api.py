from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.models import APIToken, Organization, Project, User


def signup_payload(suffix: str) -> dict[str, str]:
    return {
        "email": f"owner-{suffix}@example.com",
        "organization_name": f"Organization {suffix}",
        "project_name": "Operations",
        "timezone": "Europe/Istanbul",
        "password1": "Strong-Password-For-Tests-987",
        "password2": "Strong-Password-For-Tests-987",
    }


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_signup_requires_email_verification(client, mailoutbox):
    response = client.post(
        reverse("signup"),
        {
            "email": "new-owner@example.com",
            "organization_name": "New Org",
            "project_name": "Privacy",
            "timezone": "Europe/Istanbul",
            "password1": "Strong-Password-For-Tests-987",
            "password2": "Strong-Password-For-Tests-987",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(email="new-owner@example.com")
    assert not user.is_active
    assert user.email_verified_at is None
    assert Organization.objects.filter(owner=user).count() == 1
    assert Project.objects.filter(organization__owner=user).count() == 1
    assert len(mailoutbox) == 1
    token_match = re.search(r"/verify/([^/]+)/", mailoutbox[0].body)
    assert token_match is not None
    raw_token = token_match.group(1)
    verified = client.get(reverse("verify_email", args=[raw_token]))
    assert verified.status_code == 302
    verified_user = User.objects.get(pk=user.pk)
    assert verified_user.is_active
    assert verified_user.email_verified_at is not None


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SIGNUP_RATE_LIMIT=1,
    TRUSTED_PROXY_IPS={"127.0.0.1"},
)
def test_signup_rate_limit_distinguishes_clients_behind_trusted_proxy(client):
    first = client.post(
        reverse("signup"),
        signup_payload("proxy-a"),
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REAL_IP="203.0.113.10",
    )
    second = client.post(
        reverse("signup"),
        signup_payload("proxy-b"),
        REMOTE_ADDR="127.0.0.1",
        HTTP_X_REAL_IP="203.0.113.11",
    )
    assert first.status_code == 302
    assert second.status_code == 302


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SIGNUP_RATE_LIMIT=1,
    TRUSTED_PROXY_IPS={"127.0.0.1"},
)
def test_signup_does_not_trust_forwarded_ip_from_untrusted_peer(client):
    first = client.post(
        reverse("signup"),
        signup_payload("untrusted-a"),
        REMOTE_ADDR="198.51.100.9",
        HTTP_X_REAL_IP="203.0.113.20",
    )
    second = client.post(
        reverse("signup"),
        signup_payload("untrusted-b"),
        REMOTE_ADDR="198.51.100.9",
        HTTP_X_REAL_IP="203.0.113.21",
    )
    assert first.status_code == 302
    assert second.status_code == 429


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_failed_initial_verification_delivery_can_be_resend_safely(client, mailoutbox):
    with patch("inbox.views.send_mail", side_effect=RuntimeError("temporary SES failure")):
        signup = client.post(reverse("signup"), signup_payload("delivery-retry"))
    assert signup.status_code == 302
    user = User.objects.get(email="owner-delivery-retry@example.com")
    assert not user.is_active

    resend = client.post(
        reverse("verification_resend"),
        {"email": user.email},
        REMOTE_ADDR="203.0.113.44",
    )
    assert resend.status_code == 302
    assert len(mailoutbox) == 1
    token_match = re.search(r"/verify/([^/]+)/", mailoutbox[0].body)
    assert token_match is not None
    raw_token = token_match.group(1)
    verified = client.get(reverse("verify_email", args=[raw_token]))
    assert verified.status_code == 302
    verified_user = User.objects.get(pk=user.pk)
    assert verified_user.is_active
    assert verified_user.email_verified_at is not None


@pytest.mark.django_db
@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    VERIFICATION_RESEND_RATE_LIMIT=1,
)
def test_verification_resend_is_generic_and_rate_limited(client, mailoutbox):
    first = client.post(
        reverse("verification_resend"),
        {"email": "unknown@example.com"},
        REMOTE_ADDR="203.0.113.55",
    )
    second = client.post(
        reverse("verification_resend"),
        {"email": "unknown@example.com"},
        REMOTE_ADDR="203.0.113.55",
    )
    assert first.status_code == 302
    assert second.status_code == 429
    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_cross_tenant_web_object_returns_404(client, owner, organization, project):
    client.force_login(owner)
    other_owner = User.objects.create_user(
        email="other@example.com",
        password="Different-Strong-Password-123",
        email_verified_at=timezone.now(),
    )
    other_org = Organization.objects.create(owner=other_owner, name="Other", slug="other")
    other_project = Project.objects.create(organization=other_org, name="Other", slug="other")
    from inbox.models import Conversation

    now = timezone.now()
    hidden = Conversation.objects.create(
        organization=other_org,
        project=other_project,
        subject="Hidden",
        first_message_at=now,
        last_message_at=now,
    )
    response = client.get(reverse("conversation_detail", args=[hidden.id]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_api_bearer_scope_and_cross_tenant_404(client, owner, organization, project):
    _, raw = APIToken.issue(
        organization=organization,
        owner=owner,
        name="Read only",
        scopes=[APIToken.Scope.READ],
    )
    response = client.get(
        f"/api/v1/organizations/{organization.id}/projects",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(project.id)
    forbidden = client.post(
        f"/api/v1/organizations/{organization.id}/projects",
        data={"name": "No write"},
        content_type="application/json",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "insufficient_scope"
    assert forbidden.json()["request_id"]

    other_id = "11111111-1111-4111-8111-111111111111"
    hidden = client.get(
        f"/api/v1/organizations/{other_id}/projects",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "not_found"
