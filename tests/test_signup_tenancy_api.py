from __future__ import annotations

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.models import APIToken, Domain, User


def signup_payload(suffix: str) -> dict[str, str]:
    return {"email": f"owner-{suffix}@example.com"}


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
    assert first.status_code == 200
    assert second.status_code == 200


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
    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.django_db
def test_cross_tenant_web_object_returns_404(client, owner, organization, project):
    client.force_login(owner)
    other_owner = User.objects.create_user(
        email="other@example.com",
        password="Different-Strong-Password-123",
        email_verified_at=timezone.now(),
    )
    other_domain = Domain.objects.create(
        owner=other_owner,
        hostname="other.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        claim_expires_at=timezone.now(),
    )
    from inbox.models import Conversation

    now = timezone.now()
    hidden = Conversation.objects.create(
        domain=other_domain,
        subject="Hidden",
        first_message_at=now,
        last_message_at=now,
    )
    response = client.get(reverse("conversation_detail", args=[hidden.id]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_api_bearer_scope_and_cross_tenant_404(client, owner, organization, project):
    _, raw = APIToken.issue(
        domain=organization,
        owner=owner,
        name="Read only",
        scopes=[APIToken.Scope.READ],
    )
    response = client.get(
        "/api/v1/domains",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(organization.id)
    forbidden = client.post(
        "/api/v1/domains",
        data={"hostname": "forbidden.example", "setup_mode": "DIRECT_MX"},
        content_type="application/json",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "insufficient_scope"
    assert forbidden.json()["request_id"]

    other_id = "11111111-1111-4111-8111-111111111111"
    hidden = client.get(
        f"/api/v1/domains/{other_id}",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "not_found"
