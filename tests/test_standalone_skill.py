from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "operational-inbox"
INSTALLER = SKILL_ROOT / "scripts" / "install_codex_mcp.py"


def fake_codex_cli(tmp_path: Path, *, login_exit_code: int = 0) -> tuple[Path, Path]:
    cli = tmp_path / "codex"
    log = tmp_path / "codex-login.log"
    cli.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
log = Path(os.environ["FAKE_CODEX_LOGIN_LOG"])
if arguments == ["mcp", "--help"]:
    print("Manage external MCP servers: list get add remove login logout")
elif arguments == ["mcp", "list", "--json"]:
    status = "o_auth" if log.exists() else "unknown"
    print(json.dumps([{{"name": "operational-inbox", "auth_status": status}}]))
elif arguments[:2] == ["mcp", "login"]:
    log.write_text(json.dumps(arguments), encoding="utf-8")
    raise SystemExit({login_exit_code})
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    return cli, log


def test_standalone_skill_contract() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    manifest = json.loads((REPOSITORY_ROOT / "agent-manifest.json").read_text(encoding="utf-8"))
    skill_version = (SKILL_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    normalized_skill = " ".join(skill.split())

    assert skill.startswith("---\nname: operational-inbox\ndescription:")
    assert "TODO" not in skill
    assert "do not create an OAuth client" in skill
    assert "list_domains" in skill
    assert "Do not request a restart before checking tool availability" in skill
    assert "Never permanently delete mail" in skill
    assert "Never equate `ACCEPTED` with `DELIVERED`" in skill
    assert "resend_outbound" in skill
    assert "Do not check for or install skill updates during ordinary mailbox work" in skill
    assert "get_integration_status" in skill
    assert "Never overwrite the installed skill implicitly" in normalized_skill
    assert skill_version == manifest["skill_version"]

    assert 'display_name: "Operational Inbox"' in metadata
    assert 'icon_small: "./assets/logo.svg"' in metadata
    assert 'icon_large: "./assets/logo.svg"' in metadata
    assert 'value: "operational-inbox"' in metadata
    assert 'url: "https://operationalinbox.com/mcp"' in metadata
    assert (SKILL_ROOT / "assets" / "logo.svg").is_file()
    assert INSTALLER.is_file()


def test_one_paste_install_uses_skill_and_native_mcp() -> None:
    guide = (REPOSITORY_ROOT / "INSTALL.md").read_text(encoding="utf-8")
    normalized_guide = " ".join(guide.split())

    assert "npx -y skills@latest add onurmatik/operational-inbox" in guide
    assert "-g -s operational-inbox -y --copy" in guide
    assert "one shared copy" in guide
    assert "identify the current" in guide
    assert "For any other client" in guide
    assert "install_codex_mcp.py" in guide
    assert "codex mcp login --scopes read,write,manage_domains,send operational-inbox" in guide
    assert "CLI bundled with the macOS Codex" in guide
    assert "do not improvise another OAuth flow" in normalized_guide
    assert "call `list_domains` for a read-only connection check" in guide
    assert "call `get_integration_status`" in normalized_guide
    assert "Do not check for or install updates during ordinary mailbox work" in normalized_guide
    assert "Never overwrite the skill implicitly" in guide
    assert "do not ask the user to reload or restart anything" in normalized_guide
    assert "Do not prescribe a universal restart for other agents" in normalized_guide
    assert "Stop at the reload boundary" not in guide


def test_codex_mcp_installer_is_additive_and_idempotent(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-test"\n', encoding="utf-8")
    cli, login_log = fake_codex_cli(tmp_path)
    environment = {**os.environ, "FAKE_CODEX_LOGIN_LOG": str(login_log)}

    first = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [
            sys.executable,
            str(INSTALLER),
            "--config",
            str(config),
            "--codex-cli",
            str(cli),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    first_text = config.read_text(encoding="utf-8")
    second = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [
            sys.executable,
            str(INSTALLER),
            "--config",
            str(config),
            "--codex-cli",
            str(cli),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    document = tomllib.loads(first_text)
    server = document["mcp_servers"]["operational-inbox"]
    assert document["model"] == "gpt-test"
    assert server["url"] == "https://operationalinbox.com/mcp"
    assert server["auth"] == "oauth"
    assert server["scopes"] == ["read", "write", "manage_domains", "send"]
    assert server["default_tools_approval_mode"] == "writes"
    assert config.read_text(encoding="utf-8") == first_text
    assert "Configured Operational Inbox" in first.stdout
    assert "OAuth completed" in first.stdout
    assert "Call list_domains in the current Codex task" in first.stdout
    assert "restart only if the MCP tools are unavailable" in first.stdout
    assert "already configured" in second.stdout
    assert "OAuth is already configured" in second.stdout
    assert tomllib.loads(first_text)["mcp_servers"]["operational-inbox"]["auth"] == "oauth"
    assert login_log.exists()
    assert (
        login_log.read_text(encoding="utf-8")
        == '["mcp", "login", "--scopes", "read,write,manage_domains,send", "operational-inbox"]'
    )


def test_codex_mcp_installer_preserves_config_when_oauth_is_cancelled(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    cli, login_log = fake_codex_cli(tmp_path, login_exit_code=7)

    result = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [
            sys.executable,
            str(INSTALLER),
            "--config",
            str(config),
            "--codex-cli",
            str(cli),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE_CODEX_LOGIN_LOG": str(login_log)},
    )

    assert result.returncode == 7
    assert "OAuth did not complete" in result.stderr
    assert (
        tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["operational-inbox"]["url"]
        == "https://operationalinbox.com/mcp"
    )


def test_codex_mcp_installer_makes_restart_a_settings_fallback(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    missing_cli = tmp_path / "missing-codex"

    result = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [
            sys.executable,
            str(INSTALLER),
            "--config",
            str(config),
            "--codex-cli",
            str(missing_cli),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Open Settings > MCP servers" in result.stdout
    assert "Restart Codex only if" in result.stdout


def test_codex_mcp_installer_refuses_a_conflicting_server(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    original = '[mcp_servers.operational-inbox]\nurl = "https://unexpected.example/mcp"\n'
    config.write_text(original, encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [sys.executable, str(INSTALLER), "--config", str(config)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert config.read_text(encoding="utf-8") == original
