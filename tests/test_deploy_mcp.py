from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_mcp_runtime_contract_uses_a_dedicated_port_and_entrypoint():
    unit = (PROJECT_ROOT / ".deploy/systemd/operationalinbox-mcp.service").read_text()
    fabfile = (PROJECT_ROOT / ".deploy/fabfile.py").read_text()

    assert "operational_inbox.mcp_asgi:application" in unit
    assert "--port 8012" in unit
    assert "deadline = time.monotonic() + 30" in fabfile
    assert "timeout=min(2, remaining)" in fabfile
    assert "MCP did not become ready within 30 seconds." in fabfile
    assert "finally:\n            restart_web_runtime(connection)" in fabfile
    assert "if not mcp_verified:" in fabfile
    assert 'systemctl restart {quote(MCP_SERVICE)}", warn=True' in fabfile
