from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills" / "operational-inbox"
INSTALLER = SKILL_ROOT / "scripts" / "install_codex_mcp.py"


def test_standalone_skill_contract() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert skill.startswith("---\nname: operational-inbox\ndescription:")
    assert "TODO" not in skill
    assert "do not create an OAuth client" in skill
    assert "list_domains" in skill
    assert "Never permanently delete mail" in skill
    assert "Never equate `ACCEPTED` with `DELIVERED`" in skill
    assert "resend_outbound" in skill

    assert 'display_name: "Operational Inbox"' in metadata
    assert 'icon_small: "./assets/logo.svg"' in metadata
    assert 'icon_large: "./assets/logo.svg"' in metadata
    assert 'value: "operational-inbox"' in metadata
    assert 'url: "https://operationalinbox.com/mcp"' in metadata
    assert (SKILL_ROOT / "assets" / "logo.svg").is_file()
    assert INSTALLER.is_file()


def test_one_paste_install_uses_skill_and_native_mcp() -> None:
    guide = (REPOSITORY_ROOT / "INSTALL.md").read_text(encoding="utf-8")

    assert "npx -y skills@latest add onurmatik/operational-inbox" in guide
    assert "-g -a codex -s operational-inbox -y --copy" in guide
    assert "install_codex_mcp.py" in guide
    assert "codex mcp login operational-inbox" in guide
    assert "do not improvise another OAuth flow" in guide
    assert "Do not try to call Operational Inbox from the current task" in guide


def test_codex_mcp_installer_is_additive_and_idempotent(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-test"\n', encoding="utf-8")

    first = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [sys.executable, str(INSTALLER), "--config", str(config)],
        check=True,
        capture_output=True,
        text=True,
    )
    first_text = config.read_text(encoding="utf-8")
    second = subprocess.run(  # noqa: S603 - fixed local interpreter and checked-in script
        [sys.executable, str(INSTALLER), "--config", str(config)],
        check=True,
        capture_output=True,
        text=True,
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
    assert "already configured" in second.stdout


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
