from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from oauth2_provider.models import get_access_token_model

from inbox.models import AuditEvent
from oauth_server.cleanup import clear_expired_refresh_families, clear_stale_dynamic_clients
from oauth_server.models import OAuthApplication, OAuthRefreshFamily


def public_application() -> OAuthApplication:
    return OAuthApplication.objects.create(
        name="Codex test client",
        redirect_uris="https://client.example/callback",
        client_type=OAuthApplication.CLIENT_PUBLIC,
        authorization_grant_type=OAuthApplication.GRANT_AUTHORIZATION_CODE,
        skip_authorization=False,
        algorithm="",
    )


def oauth_access_token(application, user, *, scope: str) -> str:
    raw_token = f"oauth-access-{scope.replace(' ', '-')}"
    get_access_token_model().objects.create(
        user=user,
        application=application,
        token="",
        token_checksum=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires=timezone.now() + timedelta(minutes=10),
        scope=scope,
        resource=[settings.MCP_RESOURCE_URL],
    )
    return raw_token


@pytest.mark.django_db
def test_oauth_and_protected_resource_metadata_are_public(client):
    authorization = client.get("/.well-known/oauth-authorization-server")
    assert authorization.status_code == 200
    metadata = authorization.json()
    assert metadata["issuer"] == settings.OAUTH_ISSUER
    assert metadata["authorization_endpoint"] == f"{settings.OAUTH_ISSUER}/oauth/authorize/"
    assert metadata["token_endpoint"] == f"{settings.OAUTH_ISSUER}/oauth/token/"
    assert metadata["registration_endpoint"] == f"{settings.OAUTH_ISSUER}/oauth/register/"
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is False

    protected = client.get("/.well-known/oauth-protected-resource/mcp")
    assert protected.status_code == 200
    assert protected.json() == {
        "resource": settings.MCP_RESOURCE_URL,
        "authorization_servers": [settings.OAUTH_ISSUER],
        "scopes_supported": ["read", "write", "manage_domains", "approve_send"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": settings.MCP_DOCUMENTATION_URL,
    }


@pytest.mark.django_db
def test_dynamic_registration_accepts_only_public_pkce_clients(client):
    cache.clear()
    response = client.post(
        "/oauth/register/",
        data=json.dumps(
            {
                "client_name": "Codex",
                "redirect_uris": ["http://localhost:43123/auth/callback"],
                "response_types": ["code"],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            }
        ),
        content_type="application/json",
        REMOTE_ADDR="192.0.2.10",
    )
    assert response.status_code == 201
    assert response.json()["token_endpoint_auth_method"] == "none"  # noqa: S105
    assert "client_secret" not in response.json()
    application = OAuthApplication.objects.get(client_id=response.json()["client_id"])
    assert application.registration_source == OAuthApplication.RegistrationSource.DCR

    rejected = client.post(
        "/oauth/register/",
        data=json.dumps(
            {
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "client_secret_basic",
            }
        ),
        content_type="application/json",
        REMOTE_ADDR="192.0.2.11",
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "invalid_client_metadata"

    rejected_http = client.post(
        "/oauth/register/",
        data=json.dumps(
            {
                "redirect_uris": ["http://client.example/callback"],
                "token_endpoint_auth_method": "none",
            }
        ),
        content_type="application/json",
        REMOTE_ADDR="192.0.2.12",
    )
    assert rejected_http.status_code == 400
    assert rejected_http.json()["error"] == "invalid_client_metadata"


@pytest.mark.django_db(transaction=True)
def test_authorization_code_pkce_token_can_call_mcp(client, owner, domain):
    application = public_application()
    client.force_login(owner)
    verifier = "operational-inbox-test-pkce-verifier-0000000000000000000000"
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    parameters = {
        "response_type": "code",
        "client_id": application.client_id,
        "redirect_uri": "https://client.example/callback",
        "scope": "read write manage_domains approve_send",
        "state": "oauth-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": settings.MCP_RESOURCE_URL,
    }

    consent = client.get("/oauth/authorize/", parameters)
    assert consent.status_code == 200
    assert application.client_id in consent.content.decode()
    approved = client.post("/oauth/authorize/", {**parameters, "allow": "true"})
    assert approved.status_code == 302
    query = parse_qs(urlsplit(approved["Location"]).query)
    assert query["state"] == ["oauth-state"]
    assert query["iss"] == [settings.OAUTH_ISSUER]

    token = client.post(
        "/oauth/token/",
        {
            "grant_type": "authorization_code",
            "client_id": application.client_id,
            "code": query["code"][0],
            "redirect_uri": "https://client.example/callback",
            "code_verifier": verifier,
            "resource": settings.MCP_RESOURCE_URL,
        },
    )
    assert token.status_code == 200, token.content
    token_payload = token.json()
    stored = get_access_token_model().objects.get()
    assert stored.token == ""
    assert stored.resource == [settings.MCP_RESOURCE_URL]
    assert (
        stored.token_checksum == hashlib.sha256(token_payload["access_token"].encode()).hexdigest()
    )

    response = client.post(
        "/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_domains", "arguments": {}},
            }
        ),
        content_type="application/json",
        headers={"Authorization": f"Bearer {token_payload['access_token']}"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["items"][0]["id"] == str(domain.id)

    rotated = client.post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "client_id": application.client_id,
            "refresh_token": token_payload["refresh_token"],
            "resource": settings.MCP_RESOURCE_URL,
        },
    )
    assert rotated.status_code == 200, rotated.content
    assert rotated.json()["access_token"] != token_payload["access_token"]
    assert rotated.json()["refresh_token"] != token_payload["refresh_token"]

    refresh_replay = client.post(
        "/oauth/token/",
        {
            "grant_type": "refresh_token",
            "client_id": application.client_id,
            "refresh_token": token_payload["refresh_token"],
            "resource": settings.MCP_RESOURCE_URL,
        },
    )
    assert refresh_replay.status_code == 400
    assert refresh_replay.json()["error"] == "invalid_grant"

    replay = client.post(
        "/oauth/token/",
        {
            "grant_type": "authorization_code",
            "client_id": application.client_id,
            "code": query["code"][0],
            "redirect_uri": "https://client.example/callback",
            "code_verifier": verifier,
            "resource": settings.MCP_RESOURCE_URL,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


@pytest.mark.django_db
def test_oauth_scope_step_up_and_agent_audit(client, owner, domain, conversation):
    application = public_application()
    read_token = oauth_access_token(application, owner, scope="read")
    arguments = {
        "domain_id": str(domain.id),
        "conversation_id": str(conversation.id),
        "action": "star",
    }
    denied = client.post(
        "/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "apply_conversation_action", "arguments": arguments},
            }
        ),
        content_type="application/json",
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert denied.status_code == 403
    challenge = denied["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'error_description="Required scope: write"' in challenge

    write_token = oauth_access_token(application, owner, scope="write")
    changed = client.post(
        "/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "apply_conversation_action", "arguments": arguments},
            }
        ),
        content_type="application/json",
        headers={"Authorization": f"Bearer {write_token}"},
    )
    assert changed.status_code == 200
    assert changed.json()["result"]["structuredContent"]["changed"] is True
    event = AuditEvent.objects.get(event_type="conversation.starred")
    assert event.actor_type == AuditEvent.ActorType.AGENT
    assert event.actor_id == owner.id
    assert event.metadata["oauth_client_id"] == application.client_id


@pytest.mark.django_db
def test_oauth_housekeeping_removes_only_unreferenced_stale_records(owner):
    stale = public_application()
    stale.registration_source = OAuthApplication.RegistrationSource.DCR
    stale.save(update_fields=["registration_source", "updated"])
    OAuthApplication.objects.filter(id=stale.id).update(updated=timezone.now() - timedelta(days=31))

    referenced = public_application()
    referenced.registration_source = OAuthApplication.RegistrationSource.DCR
    referenced.save(update_fields=["registration_source", "updated"])
    OAuthApplication.objects.filter(id=referenced.id).update(
        updated=timezone.now() - timedelta(days=31)
    )
    oauth_access_token(referenced, owner, scope="read")

    expired_family = OAuthRefreshFamily.objects.create(
        family_id=uuid.uuid4(),
        issued_at=timezone.now() - timedelta(days=31),
        expires_at=timezone.now() - timedelta(days=1),
    )
    live_family = OAuthRefreshFamily.objects.create(
        family_id=uuid.uuid4(),
        issued_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=1),
    )

    assert clear_stale_dynamic_clients() == 1
    assert not OAuthApplication.objects.filter(id=stale.id).exists()
    assert OAuthApplication.objects.filter(id=referenced.id).exists()
    assert clear_expired_refresh_families() == 1
    assert not OAuthRefreshFamily.objects.filter(family_id=expired_family.family_id).exists()
    assert OAuthRefreshFamily.objects.filter(family_id=live_family.family_id).exists()


@pytest.mark.django_db
def test_public_review_and_install_surfaces(client, settings):
    assert client.get("/privacy/").status_code == 200
    assert client.get("/terms/").status_code == 200
    assert client.get("/support/").status_code == 200
    assert client.get("/mcp-docs/").status_code == 200
    assert client.get("/INSTALL.md").status_code == 200
    assert client.get("/plugin-assets/logo.png").status_code == 200
    assert client.get("/.well-known/agent-plugin/plugin.json").json()["name"] == (
        "operational-inbox"
    )
    assert client.get("/.well-known/agent-plugin/mcp.json").json()["mcpServers"]
    assert client.get("/plugins/operational-inbox/plugin.json").status_code == 200
    assert client.get("/plugins/operational-inbox/mcp.json").status_code == 200

    assert client.get("/.well-known/openai-apps-challenge").status_code == 404
    settings.OPENAI_APPS_CHALLENGE_TOKEN = "review-challenge"  # noqa: S105
    challenge = client.get("/.well-known/openai-apps-challenge")
    assert challenge.status_code == 200
    assert challenge.content == b"review-challenge"
