from __future__ import annotations

import json

import pytest

from inbox.models import APIToken, AuditEvent, MessageRecipient, OutboundMessage


def mcp_request(client, raw_token: str, method: str, params=None, *, request_id=1, origin=None):
    headers = {
        "Authorization": f"Bearer {raw_token}",
        "Accept": "application/json, text/event-stream",
    }
    if origin is not None:
        headers["Origin"] = origin
    return client.post(
        "/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        ),
        content_type="application/json",
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
    return response.json()["result"]


@pytest.mark.django_db
def test_mcp_transport_requires_bearer_and_rejects_untrusted_origins(client, owner):
    unauthenticated = client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        content_type="application/json",
    )
    assert unauthenticated.status_code == 200
    assert unauthenticated.json()["result"]["serverInfo"]["name"] == "operational-inbox"

    tool_call = client.post(
        "/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_domains", "arguments": {}},
            }
        ),
        content_type="application/json",
    )
    assert tool_call.status_code == 401
    challenge = tool_call["WWW-Authenticate"]
    assert 'error="invalid_token"' in challenge
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert tool_call.json()["result"]["_meta"]["mcp/www_authenticate"] == [challenge]
    assert client.get("/mcp").status_code == 405

    _, raw = APIToken.issue(
        domain=None,
        owner=owner,
        name="MCP read",
        scopes=[APIToken.Scope.READ],
    )
    blocked = mcp_request(
        client,
        raw,
        "initialize",
        origin="https://attacker.example",
    )
    assert blocked.status_code == 403


@pytest.mark.django_db
def test_mcp_initialize_and_tool_discovery_advertise_oauth(client, owner):
    _, read_raw = APIToken.issue(
        domain=None,
        owner=owner,
        name="MCP read",
        scopes=[APIToken.Scope.READ],
    )
    initialized = mcp_request(
        client,
        read_raw,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test"}},
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
    assert initialized.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}

    listed = mcp_request(client, read_raw, "tools/list")
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "list_domains",
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
    feed_tool = next(
        tool for tool in listed.json()["result"]["tools"] if tool["name"] == "read_message_feed"
    )
    assert "untrusted data" in feed_tool["description"]
    assert feed_tool["annotations"]["readOnlyHint"] is True
    assert feed_tool["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["read", "write", "approve_send"]}
    ]
    assert feed_tool["_meta"]["securitySchemes"] == feed_tool["securitySchemes"]


@pytest.mark.django_db
def test_mcp_feed_is_scoped_and_write_tools_enforce_scope(
    client, owner, domain, conversation, inbound_message
):
    _, read_raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Domain reader",
        scopes=[APIToken.Scope.READ],
    )
    feed = tool_call(client, read_raw, "read_message_feed", {"new_only": True})
    assert feed["isError"] is False
    assert [item["id"] for item in feed["structuredContent"]["items"]] == [str(inbound_message.id)]
    denied = tool_call(
        client,
        read_raw,
        "apply_conversation_action",
        {
            "domain_id": str(domain.id),
            "conversation_id": str(conversation.id),
            "action": "star",
        },
    )
    assert denied["isError"] is True
    assert denied["structuredContent"]["code"] == "insufficient_scope"

    _, write_raw = APIToken.issue(
        domain=domain,
        owner=owner,
        name="Domain organizer",
        scopes=[APIToken.Scope.WRITE],
    )
    changed = tool_call(
        client,
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


@pytest.mark.django_db
def test_mcp_domain_scoping_hides_other_owner_conversations(
    client, owner, domain, conversation, django_user_model
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
        client,
        raw,
        "get_conversation",
        {"domain_id": str(other_domain.id), "conversation_id": str(conversation.id)},
    )
    assert hidden["isError"] is True
    assert hidden["structuredContent"]["code"] == "not_found"


@pytest.mark.django_db
def test_mcp_agent_authored_draft_requires_exact_approval(
    client, owner, domain, conversation, inbound_message
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
        client,
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
        client,
        raw,
        "approve_and_send_reply",
        {
            "domain_id": str(domain.id),
            "draft_id": created["id"],
            "revision_id": created["revision_id"],
            "content_hash": stale_hash,
        },
    )
    assert rejected["isError"] is True
    assert rejected["structuredContent"]["code"] == "stale_revision"
    assert not OutboundMessage.objects.exists()

    approved = tool_call(
        client,
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
