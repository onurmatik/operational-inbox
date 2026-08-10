from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_PATH = PROJECT_ROOT / ".deploy" / "scripts" / "install_mcp_proxy.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_mcp_proxy", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_proxy_installer_is_idempotent_and_keeps_a_backup(tmp_path):
    installer = load_installer()
    site = tmp_path / "operationalinbox.conf"
    original = f"server {{\n{installer.LOCAL_ROUTES_MARKER}\n}}\n"
    site.write_text(original)
    include = Path("/etc/nginx/snippets/operationalinbox-mcp.conf")

    assert installer.install_include(site, include) is True
    first_install = site.read_text()
    assert f"    include {include};" in first_install
    assert site.with_suffix(".conf.pre-operationalinbox-mcp").read_text() == original

    assert installer.install_include(site, include) is False
    assert site.read_text() == first_install


def test_mcp_proxy_installer_fails_closed_without_the_expected_marker(tmp_path):
    installer = load_installer()
    site = tmp_path / "operationalinbox.conf"
    site.write_text("server {}\n")

    with pytest.raises(RuntimeError, match="found 0"):
        installer.install_include(site, Path("/etc/nginx/snippets/operationalinbox-mcp.conf"))


def test_mcp_runtime_contract_uses_a_dedicated_port_and_entrypoint():
    unit = (PROJECT_ROOT / ".deploy/systemd/operationalinbox-mcp.service").read_text()
    nginx = (PROJECT_ROOT / ".deploy/nginx/operationalinbox-mcp.conf").read_text()

    assert "operational_inbox.mcp_asgi:application" in unit
    assert "--port 8012" in unit
    assert "proxy_pass http://127.0.0.1:8012/mcp;" in nginx
