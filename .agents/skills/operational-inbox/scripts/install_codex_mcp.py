#!/usr/bin/env python3
"""Add Operational Inbox to Codex's native MCP configuration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

SERVER_NAME = "operational-inbox"
SERVER_URL = "https://operationalinbox.com/mcp"
OAUTH_SCOPES = ("read", "write", "manage_domains", "send")
BEGIN_MARKER = "# BEGIN Operational Inbox managed MCP"
END_MARKER = "# END Operational Inbox managed MCP"
MANAGED_BLOCK = f"""{BEGIN_MARKER}
[mcp_servers.{SERVER_NAME}]
url = \"{SERVER_URL}\"
auth = \"oauth\"
oauth_resource = \"{SERVER_URL}\"
scopes = [{", ".join(f'"{scope}"' for scope in OAUTH_SCOPES)}]
enabled = true
default_tools_approval_mode = \"writes\"
{END_MARKER}
"""
MACOS_BUNDLED_CODEX_PATHS = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
    Path.home() / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex",
    Path.home() / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex",
)


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
    parser.add_argument("--codex-cli", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--skip-login", action="store_true", help=argparse.SUPPRESS)
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


def configured_bearer(server: dict[str, object]) -> bool:
    if server.get("bearer_token_env_var"):
        return True
    for field in ("http_headers", "env_http_headers"):
        headers = server.get(field)
        if isinstance(headers, dict) and any(
            str(name).lower() == "authorization" for name in headers
        ):
            return True
    return False


def codex_cli_candidates(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit.expanduser()]

    candidates: list[Path] = []
    path_cli = shutil.which("codex")
    if path_cli:
        candidates.append(Path(path_cli))
    app_cli = os.environ.get("CODEX_CLI_PATH")
    if app_cli:
        candidates.append(Path(app_cli).expanduser())
    candidates.extend(MACOS_BUNDLED_CODEX_PATHS)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = os.path.realpath(candidate)
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    return unique


def supports_native_mcp(candidate: Path) -> bool:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    try:
        result = subprocess.run(  # noqa: S603 - validated executable candidate.
            [str(candidate), "mcp", "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 0 and "Manage external MCP servers" in output and "login" in output


def find_codex_cli(explicit: Path | None) -> Path | None:
    return next(
        (
            candidate
            for candidate in codex_cli_candidates(explicit)
            if supports_native_mcp(candidate)
        ),
        None,
    )


def oauth_is_configured(cli: Path) -> bool:
    try:
        result = subprocess.run(  # noqa: S603 - validated Codex CLI.
            [str(cli), "mcp", "list", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        servers = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    if not isinstance(servers, list):
        return False
    for server in servers:
        if isinstance(server, dict) and server.get("name") == SERVER_NAME:
            status = str(server.get("auth_status", "")).lower().replace("-", "_")
            return status in {"oauth", "o_auth"}
    return False


def login_with_native_oauth(cli: Path) -> int:
    if oauth_is_configured(cli):
        print("Operational Inbox OAuth is already configured for Codex.")
        return 0

    print(f"Starting native Operational Inbox OAuth with {cli}.")
    try:
        result = subprocess.run(  # noqa: S603 - validated Codex CLI.
            [
                str(cli),
                "mcp",
                "login",
                "--scopes",
                ",".join(OAUTH_SCOPES),
                SERVER_NAME,
            ],
            check=False,
        )
    except OSError as error:
        print(f"Could not start Codex OAuth: {error}", file=os.sys.stderr)
        return 1
    if result.returncode != 0:
        print(
            "Codex OAuth did not complete. The MCP configuration was preserved; "
            "rerun this installer to try again.",
            file=os.sys.stderr,
        )
        return result.returncode or 1
    print("Operational Inbox OAuth completed. Restart Codex and start a new task.")
    return 0


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
    else:
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

    if args.skip_login:
        print("Skipped native OAuth login as requested.")
        return 0
    if existing is not None and configured_bearer(existing):
        print(
            "Operational Inbox already uses configured bearer authentication; leaving it unchanged."
        )
        return 0

    cli = find_codex_cli(args.codex_cli)
    if cli is None:
        print(
            "No MCP-capable Codex CLI was found. Restart Codex, then open "
            "Settings > MCP servers > operational-inbox and select Authenticate."
        )
        return 0
    return login_with_native_oauth(cli)


if __name__ == "__main__":
    raise SystemExit(main())
