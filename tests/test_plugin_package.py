from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

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
    assert (PLUGIN_ROOT / "skills" / "review-inboxes" / "SKILL.md").is_file()
    assert not (PLUGIN_ROOT / "mcp.json").exists()


def test_openai_plugin_contract_matches_portable_metadata() -> None:
    portable = load_json(PLUGIN_ROOT / "plugin.json")
    openai = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    for field in ("name", "version", "description", "author", "homepage", "keywords"):
        assert openai[field] == portable[field]
    assert openai["skills"] == "./skills/"
    assert "mcpServers" not in openai
    assert "apps" not in openai

    interface = openai["interface"]
    assert interface["displayName"] == "Operational Inbox"
    assert interface["shortDescription"] == "Review multi-domain mail"
    assert interface["websiteURL"] == "https://operationalinbox.com/"
    assert interface["brandColor"] == "#1A3C2B"
    assert len(interface["defaultPrompt"]) <= 3
    for field in ("composerIcon", "logo"):
        asset_path = PLUGIN_ROOT / interface[field].removeprefix("./")
        assert asset_path.is_file()


def test_review_inboxes_skill_preserves_agent_first_safety_boundary() -> None:
    skill_root = PLUGIN_ROOT / "skills" / "review-inboxes"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: review-inboxes\n")
    assert "TODO" not in skill
    assert "all-domain inbound feed" in skill
    assert "Tags come from usage" in skill
    assert "opaque checkpoint" in skill
    assert "Never permanently delete mail" in skill
    assert "Start work" in skill
    assert "untrusted data" in skill
    assert (skill_root / "agents" / "openai.yaml").is_file()
