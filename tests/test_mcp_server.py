from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone
from jsonschema import Draft202012Validator, FormatChecker
from oauth2_provider.models import get_access_token_model
from starlette.testclient import TestClient

from inbox.mcp_application import create_mcp_application
from inbox.mcp_server import MCP_TOOLS
from inbox.models import (
    APIToken,
    AuditEvent,
    Domain,
    DomainDNSRecord,
    DurableJob,
    InboundRoute,
    MessageRecipient,
    OutboundMessage,
)
from inbox.services.domains import classify_domain_routing
from oauth_server.models import OAuthApplication


@pytest.fixture
def mcp_client():
    with TestClient(create_mcp_application(), base_url="http://testserver") as client:
        yield client


def mcp_request(
    client, raw_token: str | None, method: str, params=None, *, request_id=1, origin=None
):
    protocol_version = "2025-11-25" if method == "initialize" else "2026-07-28"
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    request_params = dict(params or {})
    if method != "initialize":
        headers["MCP-Method"] = method
        if method == "tools/call" and isinstance(request_params.get("name"), str):
            headers["MCP-Name"] = request_params["name"]
        request_params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": protocol_version,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0"},
        }
    if raw_token is not None:
        headers["Authorization"] = f"Bearer {raw_token}"
    if origin is not None:
        headers["Origin"] = origin
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": request_params,
        },
        headers=headers,
    )


def tool_call(client, raw_token: str, name: str, arguments: dict, *, request_id=1):
    response = mcp_request(
        client,
        raw_token,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=request_id,
    )
    assert response.status_code == 200
    result = response.json()["result"]
    if not result["isError"]:
        descriptor = next(tool for tool in MCP_TOOLS if tool["name"] == name)
        Draft202012Validator(
            descriptor["outputSchema"],
            format_checker=FormatChecker(),
        ).validate(result["structuredContent"])
    return result


def tool_error_payload(result: dict) -> dict:
    assert result["isError"] is True
    assert "structuredContent" not in result
    return json.loads(result["content"][0]["text"])


def oauth_access_token(owner, *, scope: str) -> str:
    application = OAuthApplication.objects.create(
        name="MCP domain setup client",
        redirect_uris="https://client.example/callback",
        client_type=OAuthApplication.CLIENT_PUBLIC,
        authorization_grant_type=OAuthApplication.GRANT_AUTHORIZATION_CODE,
        skip_authorization=False,
        algorithm="",
    )
    raw_token = f"oauth-domain-setup-{scope.replace(' ', '-')}"
    get_access_token_model().objects.create(
        user=owner,
        application=application,
        token="",
        token_checksum=hashlib.sha256(raw_token.encode()).hexdigest(),
        expires=timezone.now() + timedelta(minutes=10),
        scope=scope,
        resource=[settings.MCP_RESOURCE_URL],
    )
    return raw_token


@pytest.mark.django_db(transaction=True)
def test_mcp_transport_requires_bearer_and_rejects_untrusted_origins(mcp_client, owner):
    unauthenticated = mcp_request(
        mcp_client,
        None,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    )
    assert unauthenticated.status_code == 200
    assert unauthenticated.json()["result"]["serverInfo"]["name"] == "operational-inbox"

    tool_response = mcp_request(
        mcp_client,
        None,
        "tools/call",
        {"name": "list_domains", "arguments": {}},
        request_id=2,
    )
    assert tool_response.status_code == 200
    challenge = tool_response.json()["result"]["_meta"]["mcp/www_authenticate"][0]
    assert 'error="invalid_token"' in challenge
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert tool_response.json()["result"]["isError"] is True
    assert mcp_client.get("/mcp", headers={"Accept": "application/json"}).status_code == 406
    preflight = mcp_client.options(
        "/mcp",
        headers={
            "Origin": "https://chatgpt.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,mcp-method,mcp-name,mcp-protocol-version"
            ),
        },
    )
    assert preflight.status_code == 200
    assert "mcp-method" in preflight.headers["Access-Control-Allow-Headers"].lower()

    _, raw = APIToken.issue(
        domain=None,
        owner=owner,
        name="MCP read",
        scopes=[APIToken.Scope.READ],
    )
    blocked = mcp_request(
        mcp_client,
        raw,
        "initialize",
        origin="https://attacker.example",
    )
    assert blocked.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_mcp_initialize_and_tool_discovery_advertise_oauth(mcp_client, owner):
    _, read_raw = APIToken.issue(
        domain=None,
        owner=owner,
        name="MCP read",
        scopes=[APIToken.Scope.READ],
    )
    initialized = mcp_request(
        mcp_client,
        read_raw,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
    assert initialized.json()["result"]["capabilities"]["tools"] == {"listChanged": False}

    listed = mcp_request(mcp_client, read_raw, "tools/list")
    tools = listed.json()["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "list_domains",
        "inspect_domain_dns",
        "start_domain_onboarding",
        "get_domain_setup_plan",
        "request_domain_dns_check",
        "read_message_feed",
        "get_conversation",
        "add_conversation_tag",
        "remove_conversation_tag",
        "apply_conversation_action",
        "get_domain_health",
        "get_outbound_status",
        "list_audit_events",
        "create_reply_draft",
        "get_reply_draft",
        "revise_reply_draft",
        "approve_and_send_reply",
        "resend_outbound",
    }
    for tool in tools:
        assert {"readOnlyHint", "destructiveHint", "openWorldHint"} <= set(tool["annotations"])
        Draft202012Validator.check_schema(tool["outputSchema"])

    feed_tool = next(tool for tool in tools if tool["name"] == "read_message_feed")
    assert "untrusted data" in feed_tool["description"]
    assert feed_tool["annotations"]["readOnlyHint"] is True
    assert feed_tool["annotations"]["destructiveHint"] is False
    assert feed_tool["securitySchemes"] == [{"type": "oauth2", "scopes": ["read"]}]
    assert feed_tool["_meta"]["securitySchemes"] == feed_tool["securitySchemes"]
    inspect_tool = next(tool for tool in tools if tool["name"] == "inspect_domain_dns")
    assert inspect_tool["securitySchemes"] == [{"type": "oauth2", "scopes": ["manage_domains"]}]
    assert inspect_tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }


@pytest.mark.django_db(transaction=True)
def test_mcp_domain_setup_inspects_starts_plans_and_queues_check(mcp_client, monkeypatch, owner):
    inspection = classify_domain_routing([], has_operational_inbox_claim=False)
    monkeypatch.setattr("inbox.mcp_server.inspect_domain_routing", lambda hostname: inspection)
    monkeypatch.setattr("inbox.services.domains.inspect_mx", lambda hostname: [])
    raw = oauth_access_token(owner, scope="manage_domains read")

    inspected = tool_call(
        mcp_client,
        raw,
        "inspect_domain_dns",
        {"hostname": "New-Inbox.Example."},
    )["structuredContent"]
    assert inspected == {
        "hostname": "new-inbox.example",
        "has_existing_mx": False,
        "mx_classification": "NO_MX",
        "has_operational_inbox_claim": False,
        "recommended_setup_mode": Domain.SetupMode.DIRECT_MX,
        "requires_explicit_choice": False,
        "mx_records": [],
    }

    started = tool_call(
        mcp_client,
        raw,
        "start_domain_onboarding",
        {
            "hostname": "New-Inbox.Example.",
            "setup_mode": Domain.SetupMode.DIRECT_MX,
        },
    )["structuredContent"]
    domain = Domain.objects.get(id=started["id"])
    assert domain.hostname == "new-inbox.example"
    assert domain.status == Domain.Status.PROVISIONING
    assert DurableJob.objects.filter(kind="provision_domain", domain=domain).count() == 1

    pending_plan = tool_call(
        mcp_client,
        raw,
        "get_domain_setup_plan",
        {"domain_id": str(domain.id)},
    )["structuredContent"]
    assert pending_plan["plan_ready"] is False
    assert pending_plan["records_to_upsert"] == []

    domain.status = Domain.Status.PENDING_DNS
    domain.save(update_fields=("status", "updated_at"))
    record = DomainDNSRecord.objects.create(
        domain=domain,
        purpose=DomainDNSRecord.Purpose.MX,
        record_type="MX",
        name=domain.hostname,
        value="inbound-smtp.us-east-1.amazonaws.com",
        priority=10,
        ttl=300,
    )
    ready_plan = tool_call(
        mcp_client,
        raw,
        "get_domain_setup_plan",
        {"domain_id": str(domain.id)},
    )["structuredContent"]
    assert ready_plan["plan_ready"] is True
    assert ready_plan["setup_generation"] == 1
    assert ready_plan["records_to_upsert"][0]["id"] == str(record.id)
    assert ready_plan["records_to_upsert"][0]["ttl"] == 300
    assert ready_plan["existing_mx"] == []
    assert ready_plan["records_to_preserve"] == []
    assert ready_plan["must_preserve_existing_mx"] is False
    assert ready_plan["requires_explicit_confirmation"] is False

    queued = tool_call(
        mcp_client,
        raw,
        "request_domain_dns_check",
        {"domain_id": str(domain.id)},
    )["structuredContent"]
    assert queued["status"] == "queued"
    assert DurableJob.objects.filter(id=queued["job_id"], kind="dns_check").exists()

    provider_domain = Domain.objects.create(
        owner=owner,
        hostname="provider-forward.example",
        setup_mode=Domain.SetupMode.PROVIDER_FORWARD,
        status=Domain.Status.PENDING_DNS,
        existing_mx=[{"preference": 10, "exchange": "mx.provider.example"}],
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    InboundRoute.objects.create(
        domain=provider_domain,
        kind=InboundRoute.Kind.FORWARDING_ALIAS,
        local_part="route-provider-forward",
        address="route-provider-forward@inbound.example.net",
    )
    DomainDNSRecord.objects.create(
        domain=provider_domain,
        purpose=DomainDNSRecord.Purpose.OWNERSHIP,
        record_type="TXT",
        name="_operational-inbox-claim.provider-forward.example",
        value="claim-token",
    )
    provider_plan = tool_call(
        mcp_client,
        raw,
        "get_domain_setup_plan",
        {"domain_id": str(provider_domain.id)},
    )["structuredContent"]
    assert provider_plan["existing_mx"] == [{"preference": 10, "exchange": "mx.provider.example"}]
    assert provider_plan["records_to_preserve"] == provider_plan["existing_mx"]
    assert provider_plan["must_preserve_existing_mx"] is True
    assert provider_plan["provider_forwarding_target"] == (
        "route-provider-forward@inbound.example.net"
    )


@pytest.mark.django_db(transaction=True)
def test_mcp_domain_setup_requires_confirmation_for_non_recommended_route(
    mcp_client, monkeypatch, owner
):
    inspection = classify_domain_routing(
        [],
        has_operational_inbox_claim=False,
    )
    monkeypatch.setattr("inbox.mcp_server.inspect_domain_routing", lambda hostname: inspection)
    raw = oauth_access_token(owner, scope="manage_domains")

    rejected = tool_call(
        mcp_client,
        raw,
        "start_domain_onboarding",
        {
            "hostname": "route-choice.example",
            "setup_mode": Domain.SetupMode.PROVIDER_FORWARD,
        },
    )

    assert tool_error_payload(rejected)["code"] == "routing_choice_confirmation_required"
    assert not Domain.objects.filter(hostname="route-choice.example").exists()


@pytest.mark.django_db(transaction=True)
def test_mcp_feed_is_scoped_and_write_tools_enforce_scope(
    mcp_client, owner, domain, conversation, inbound_message
):
    _, read_raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Domain reader",
        scopes=[APIToken.Scope.READ],
    )
    feed = tool_call(mcp_client, read_raw, "read_message_feed", {"new_only": True})
    assert feed["isError"] is False
    assert [item["id"] for item in feed["structuredContent"]["items"]] == [str(inbound_message.id)]
    denied = tool_call(
        mcp_client,
        read_raw,
        "apply_conversation_action",
        {
            "domain_id": str(domain.id),
            "conversation_id": str(conversation.id),
            "action": "star",
        },
    )
    assert tool_error_payload(denied)["code"] == "insufficient_scope"

    _, write_raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Domain organizer",
        scopes=[APIToken.Scope.WRITE],
    )
    changed = tool_call(
        mcp_client,
        write_raw,
        "apply_conversation_action",
        {
            "domain_id": str(domain.id),
            "conversation_id": str(conversation.id),
            "action": "star",
        },
    )
    assert changed["isError"] is False
    assert changed["structuredContent"]["changed"] is True
    conversation.refresh_from_db()
    assert conversation.starred_at is not None
    assert AuditEvent.objects.filter(
        domain=domain,
        event_type="conversation.starred",
        actor_type=AuditEvent.ActorType.AGENT,
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_mcp_domain_scoping_hides_other_owner_conversations(
    mcp_client, owner, domain, conversation, django_user_model
):
    other_owner = django_user_model.objects.create_user(
        email="other-mcp@example.com",
        password="Different-Strong-Password-123",
        email_verified_at=conversation.last_message_at,
    )
    other_domain = domain.__class__.objects.create(
        owner=other_owner,
        hostname="other-mcp.example",
        setup_mode=domain.setup_mode,
        status=domain.status,
        claim_expires_at=domain.claim_expires_at,
    )
    _, raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Scoped MCP",
        scopes=[APIToken.Scope.READ],
    )
    hidden = tool_call(
        mcp_client,
        raw,
        "get_conversation",
        {"domain_id": str(other_domain.id), "conversation_id": str(conversation.id)},
    )
    assert tool_error_payload(hidden)["code"] == "not_found"


@pytest.mark.django_db(transaction=True)
def test_mcp_domain_scoped_token_cannot_inspect_another_hostname(
    mcp_client, monkeypatch, owner, domain
):
    _, raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Scoped domain manager",
        scopes=[APIToken.Scope.MANAGE_DOMAINS],
    )

    def unexpected_lookup(hostname):
        raise AssertionError(f"unexpected DNS lookup for {hostname}")

    monkeypatch.setattr("inbox.mcp_server.inspect_domain_routing", unexpected_lookup)
    hidden = tool_call(
        mcp_client,
        raw,
        "inspect_domain_dns",
        {"hostname": "other.example"},
    )

    assert tool_error_payload(hidden)["code"] == "not_found"


@pytest.mark.django_db(transaction=True)
def test_mcp_agent_authored_draft_requires_exact_approval(
    mcp_client, owner, domain, conversation, inbound_message
):
    MessageRecipient.objects.create(
        domain=domain,
        message=inbound_message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address="privacy@example.com",
        is_routing_recipient=True,
    )
    _, raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Reply agent",
        scopes=[APIToken.Scope.READ, APIToken.Scope.WRITE, APIToken.Scope.APPROVE_SEND],
    )
    created = tool_call(
        mcp_client,
        raw,
        "create_reply_draft",
        {
            "domain_id": str(domain.id),
            "conversation_id": str(conversation.id),
            "subject": "Re: Privacy request",
            "body_text": "We received your request.",
        },
    )["structuredContent"]
    assert created["subject"] == "Re: Privacy request"
    assert len(created["content_hash"]) == 64

    stale_hash = "0" * 64
    rejected = tool_call(
        mcp_client,
        raw,
        "approve_and_send_reply",
        {
            "domain_id": str(domain.id),
            "draft_id": created["id"],
            "revision_id": created["revision_id"],
            "content_hash": stale_hash,
        },
    )
    assert tool_error_payload(rejected)["code"] == "stale_revision"
    assert not OutboundMessage.objects.exists()

    approved = tool_call(
        mcp_client,
        raw,
        "approve_and_send_reply",
        {
            "domain_id": str(domain.id),
            "draft_id": created["id"],
            "revision_id": created["revision_id"],
            "content_hash": created["content_hash"],
        },
    )
    assert approved["isError"] is False
    assert approved["structuredContent"]["status"] == OutboundMessage.Status.QUEUED
    assert OutboundMessage.objects.count() == 1
