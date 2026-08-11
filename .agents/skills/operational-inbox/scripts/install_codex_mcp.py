#!/usr/bin/env python3
"""Add Operational Inbox to Codex's native MCP configuration."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
import tomllib
from pathlib import Path

SERVER_NAME = "operational-inbox"
SERVER_URL = "https://operationalinbox.com/mcp"
BEGIN_MARKER = "# BEGIN Operational Inbox managed MCP"
END_MARKER = "# END Operational Inbox managed MCP"
MANAGED_BLOCK = f"""{BEGIN_MARKER}
[mcp_servers.{SERVER_NAME}]
url = \"{SERVER_URL}\"
auth = \"oauth\"
oauth_resource = \"{SERVER_URL}\"
scopes = [\"read\", \"write\", \"manage_domains\", \"send\"]
enabled = true
default_tools_approval_mode = \"writes\"
{END_MARKER}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure Operational Inbox as a native Codex MCP server."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".codex" / "config.toml",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def read_config(path: Path) -> tuple[str, dict[str, object]]:
    if not path.exists():
        return "", {}
    text = path.read_text(encoding="utf-8")
    return text, tomllib.loads(text)


def configured_server(document: dict[str, object]) -> dict[str, object] | None:
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    server = servers.get(SERVER_NAME)
    return server if isinstance(server, dict) else None


def write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    args = parse_args()
    try:
        current_text, document = read_config(args.config)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"Cannot read valid Codex config at {args.config}: {error}") from error

    existing = configured_server(document)
    if existing is not None:
        if existing.get("url") != SERVER_URL:
            raise SystemExit(
                f"Codex already has {SERVER_NAME!r} configured with another URL; "
                "refusing to overwrite it."
            )
        print(f"Operational Inbox is already configured in {args.config}.")
        return 0

    prefix = current_text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    updated_text = prefix + MANAGED_BLOCK

    try:
        tomllib.loads(updated_text)
        write_atomically(args.config, updated_text)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"Cannot update Codex config at {args.config}: {error}") from error

    print(f"Configured Operational Inbox in {args.config}.")
    print(
        "Authenticate with `codex mcp login operational-inbox` if supported; "
        "otherwise restart Codex and use Settings > MCP servers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
