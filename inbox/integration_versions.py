from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AGENT_MANIFEST_PATH = REPOSITORY_ROOT / "agent-manifest.json"
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
VERSION_FIELDS = (
    "server_version",
    "mcp_contract_version",
    "skill_version",
    "minimum_skill_version",
)
SkillStatus = Literal[
    "unknown",
    "upgrade_required",
    "update_available",
    "current",
    "newer_than_server",
]


def _read_agent_manifest() -> dict[str, str]:
    value = json.loads(AGENT_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("agent-manifest.json must contain a JSON object.")
    manifest = cast(dict[str, Any], value)
    for field in VERSION_FIELDS:
        version = manifest.get(field)
        if not isinstance(version, str) or SEMVER_PATTERN.fullmatch(version) is None:
            raise RuntimeError(f"agent-manifest.json has an invalid {field}.")
    update_url = manifest.get("skill_update_url")
    if not isinstance(update_url, str) or not update_url.startswith("https://"):
        raise RuntimeError("agent-manifest.json has an invalid skill_update_url.")
    return {str(key): str(item) for key, item in manifest.items()}


AGENT_MANIFEST = _read_agent_manifest()
SERVER_VERSION = AGENT_MANIFEST["server_version"]
MCP_CONTRACT_VERSION = AGENT_MANIFEST["mcp_contract_version"]
LATEST_SKILL_VERSION = AGENT_MANIFEST["skill_version"]
MINIMUM_SKILL_VERSION = AGENT_MANIFEST["minimum_skill_version"]
SKILL_UPDATE_URL = AGENT_MANIFEST["skill_update_url"]


def _semver_key(version: str) -> tuple[int, int, int, tuple[tuple[int, int | str], ...]]:
    match = SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError("skill_version must be a valid semantic version.")
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key: tuple[tuple[int, int | str], ...] = ((2, 0),)
    else:
        prerelease_key = tuple(
            (0, int(identifier)) if identifier.isdigit() else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease_key


if _semver_key(MINIMUM_SKILL_VERSION) > _semver_key(LATEST_SKILL_VERSION):
    raise RuntimeError("minimum_skill_version cannot exceed skill_version.")


def skill_status(reported_skill_version: str | None) -> SkillStatus:
    if reported_skill_version is None:
        return "unknown"
    reported = _semver_key(reported_skill_version)
    minimum = _semver_key(MINIMUM_SKILL_VERSION)
    latest = _semver_key(LATEST_SKILL_VERSION)
    if reported < minimum:
        return "upgrade_required"
    if reported < latest:
        return "update_available"
    if reported == latest:
        return "current"
    return "newer_than_server"


def integration_status(reported_skill_version: str | None) -> dict[str, Any]:
    status = skill_status(reported_skill_version)
    return {
        "server_version": SERVER_VERSION,
        "mcp_contract_version": MCP_CONTRACT_VERSION,
        "latest_skill_version": LATEST_SKILL_VERSION,
        "minimum_skill_version": MINIMUM_SKILL_VERSION,
        "reported_skill_version": reported_skill_version,
        "skill_status": status,
        "upgrade_required": status == "upgrade_required",
        "update_available": status == "update_available",
        "skill_update_url": SKILL_UPDATE_URL,
    }
