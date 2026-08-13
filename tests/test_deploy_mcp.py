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


def test_postgresql_cutover_is_separate_from_normal_runtime_env_sync():
    fabfile = (PROJECT_ROOT / ".deploy" / "fabfile.py").read_text()
    migration = (
        PROJECT_ROOT / ".deploy" / "scripts" / "postgresql_migration.py"
    ).read_text()

    runtime_keys = fabfile.split("RUNTIME_ENV_KEYS = (", 1)[1].split(")", 1)[0]
    assert "DJANGO_DATABASE_URL" not in runtime_keys
    assert "def prepare_postgresql" in fabfile
    assert "def cutover_postgresql" in fabfile
    assert "result.exited == 20" in fabfile
    assert "roll-forward" in fabfile
    assert (
        'for suffix in ("deploy", "ingest", "scheduler", "dns", "retention", "backup")'
        in migration
    )
    assert "descriptor = os.open(path, os.O_RDWR)" in migration
    assert "lock_handles = []" in migration
    assert "lock_handles = acquire_locks(args.project_name, args.app_user)" in migration
    assert '"--natural-foreign"' in migration
    assert '"--natural-primary"' not in migration
    assert '"contenttypes"' in migration
    assert '"auth.permission"' in migration
    assert "validate_database_manifest" in migration
    assert "validate_postgresql_sequences" in migration
    assert '{"BEGIN;", "COMMIT;", "BEGIN", "COMMIT"}' in migration
