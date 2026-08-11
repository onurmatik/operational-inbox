from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from inbox.mcp_server import MCP_TOOLS

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "operational-inbox"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def project_version() -> str:
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def test_portable_agent_plugin_v1_contract() -> None:
    manifest = load_json(PLUGIN_ROOT / "plugin.json")

    assert manifest["$schema"] == ("https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
    assert manifest["name"] == "operational-inbox"
    assert manifest["version"] == project_version()
    assert set(manifest) == {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "keywords",
    }
    assert (PLUGIN_ROOT / "skills" / "triage-inboxes" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / "skills" / "reply-to-conversations" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / "skills" / "setup-domain" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / "skills" / "monitor-outbound-delivery" / "SKILL.md").is_file()

    mcp = load_json(PLUGIN_ROOT / "mcp.json")
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert set(mcp) == {"$schema", "mcpServers"}
    server = mcp["mcpServers"]["operational-inbox"]
    assert server == {
        "type": "streamable-http",
        "url": "https://operationalinbox.com/mcp",
    }


def test_openai_plugin_contract_matches_portable_metadata() -> None:
    portable = load_json(PLUGIN_ROOT / "plugin.json")
    openai = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    for field in ("name", "version", "description", "author", "homepage", "keywords"):
        assert openai[field] == portable[field]
    assert openai["skills"] == "./skills/"
    assert openai["mcpServers"] == "./.mcp.json"
    assert "apps" not in openai

    codex_mcp = load_json(PLUGIN_ROOT / ".mcp.json")
    assert codex_mcp["mcpServers"]["operational-inbox"] == {
        "type": "http",
        "url": load_json(PLUGIN_ROOT / "mcp.json")["mcpServers"]["operational-inbox"]["url"],
    }

    interface = openai["interface"]
    assert interface["displayName"] == "Operational Inbox"
    assert interface["shortDescription"] == "Set up, triage, and operate replies"
    assert interface["websiteURL"] == "https://operationalinbox.com/"
    assert interface["privacyPolicyURL"] == "https://operationalinbox.com/privacy/"
    assert interface["termsOfServiceURL"] == "https://operationalinbox.com/terms/"
    assert interface["brandColor"] == "#1A3C2B"
    assert len(interface["defaultPrompt"]) <= 3
    for field in ("composerIcon", "logo"):
        asset_path = PLUGIN_ROOT / interface[field].removeprefix("./")
        assert asset_path.is_file()


def test_repo_marketplace_and_mcp_registry_point_to_the_release() -> None:
    marketplace = load_json(REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json")
    assert marketplace["name"] == "personal"
    entry = next(item for item in marketplace["plugins"] if item["name"] == "operational-inbox")
    assert entry == {
        "name": "operational-inbox",
        "source": {"source": "local", "path": "./plugins/operational-inbox"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }

    registry = load_json(REPOSITORY_ROOT / "server.json")
    assert registry["version"] == project_version()
    assert registry["remotes"] == [
        {"type": "streamable-http", "url": "https://operationalinbox.com/mcp"}
    ]
    assert registry["icons"][0]["sizes"] == ["512x512"]

    integration = load_json(REPOSITORY_ROOT / "agent-manifest.json")
    assert integration["server_version"] == project_version()
    assert (
        integration["skill_version"]
        == (REPOSITORY_ROOT / ".agents" / "skills" / "operational-inbox" / "VERSION")
        .read_text(encoding="utf-8")
        .strip()
    )


def test_public_submission_covers_the_current_tool_surface() -> None:
    submission = load_json(REPOSITORY_ROOT / "chatgpt-app-submission.json")
    assert set(submission["tools"]) == {tool["name"] for tool in MCP_TOOLS}
    assert len(submission["test_cases"]) == 6
    assert len(submission["negative_test_cases"]) == 3


def test_triage_inboxes_skill_preserves_agent_first_safety_boundary() -> None:
    skill_root = PLUGIN_ROOT / "skills" / "triage-inboxes"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: triage-inboxes\n")
    assert "TODO" not in skill
    assert "all-domain inbound feed" in skill
    assert "Tags come from usage" in skill
    assert "opaque checkpoint" in skill
    assert "Never permanently delete mail" in skill
    assert "Start work" in skill
    assert "untrusted data" in skill
    assert "$reply-to-conversations" in skill
    assert (skill_root / "agents" / "openai.yaml").is_file()


def test_reply_skill_uses_delegated_exact_revision_send() -> None:
    skill_root = PLUGIN_ROOT / "skills" / "reply-to-conversations"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: reply-to-conversations\n")
    assert "second per-message approval" in skill
    assert "revision ID and hash" in skill
    assert "Never retry a failed or unknown attempt automatically" in skill
    assert "untrusted data" in skill
    assert (skill_root / "agents" / "openai.yaml").is_file()


def test_outbound_monitor_skill_keeps_retry_and_scope_focused() -> None:
    skill_root = PLUGIN_ROOT / "skills" / "monitor-outbound-delivery"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: monitor-outbound-delivery\n")
    assert "list_outbound" in skill
    assert "set_outbound_paused" in skill
    assert "Never resend automatically" in skill
    assert "bulk, marketing" in skill
    assert "TODO" not in skill
    assert (skill_root / "agents" / "openai.yaml").is_file()


def test_setup_domain_skill_preserves_dns_and_confirmation_boundaries() -> None:
    skill_root = PLUGIN_ROOT / "skills" / "setup-domain"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: setup-domain\n")
    assert "inspect_domain_dns" in skill
    assert "setup_generation" in skill
    assert "Never replace existing MX implicitly" in skill
    assert "request_domain_dns_check" in skill
    assert "provider tool" in skill
    assert "TODO" not in skill
    assert (skill_root / "agents" / "openai.yaml").is_file()
