from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from inbox.database_validation import build_database_manifest
from inbox.management.commands import backup_database

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _settings_for(database_url: str):
    script = (
        "import json; from operational_inbox import settings; "
        "print(json.dumps(settings.DATABASES['default'], default=str, sort_keys=True))"
    )
    environment = os.environ.copy()
    environment["DJANGO_DATABASE_URL"] = database_url
    return subprocess.run(  # noqa: S603 - the interpreter and script are fixed test inputs.
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )


def test_settings_preserve_sqlite_locking_options():
    result = _settings_for("sqlite:///custom.sqlite3")
    assert result.returncode == 0
    configured = json.loads(result.stdout)
    assert configured["ENGINE"] == "django.db.backends.sqlite3"
    assert configured["NAME"].endswith("/custom.sqlite3")
    assert configured["OPTIONS"] == {"timeout": 20, "transaction_mode": "IMMEDIATE"}


def test_settings_use_sqlite_fallback_for_empty_database_url():
    result = _settings_for("")
    assert result.returncode == 0
    configured = json.loads(result.stdout)
    assert configured["ENGINE"] == "django.db.backends.sqlite3"
    assert configured["NAME"].endswith("/db.sqlite3")


def test_settings_parse_postgresql_with_persistent_health_checked_connections():
    result = _settings_for("postgresql://app:secret@127.0.0.1:5432/database")
    assert result.returncode == 0
    configured = json.loads(result.stdout)
    assert configured["ENGINE"] == "django.db.backends.postgresql"
    assert configured["CONN_MAX_AGE"] == 60
    assert configured["CONN_HEALTH_CHECKS"] is True
    assert configured["HOST"] == "127.0.0.1"
    assert configured["NAME"] == "database"


def test_settings_reject_unsupported_database_scheme_without_echoing_url():
    database_url = "mysql://app:do-not-print@example.invalid/database"
    result = _settings_for(database_url)
    assert result.returncode != 0
    assert "sqlite:/// or postgresql://" in result.stderr
    assert "do-not-print" not in result.stderr


def test_sqlite_backup_command_remains_available_through_compatibility_alias(tmp_path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE example (id integer primary key, value text)")
        database.execute("INSERT INTO example (value) VALUES ('preserved')")
    encryption_key = base64.urlsafe_b64encode(b"k" * 32).decode()
    with override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": source,
            }
        },
        BACKUP_DIRECTORY=str(tmp_path / "backups"),
        BACKUP_ENCRYPTION_KEY=encryption_key,
        AWS_INGRESS_BUCKET="",
    ):
        call_command("backup_sqlite", verbosity=0)
    backups = list((tmp_path / "backups").glob("*.sqlite3.aesgcm"))
    assert len(backups) == 1
    assert backups[0].read_bytes().startswith(backup_database.MAGIC)
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_postgresql_snapshot_uses_password_only_in_child_environment(monkeypatch, tmp_path):
    destination = tmp_path / "database.pgdump"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "/usr/bin/pg_dump":
            output = next(
                value.split("=", 1)[1] for value in command if value.startswith("--file=")
            )
            Path(output).write_bytes(b"postgresql custom dump")
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(backup_database.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backup_database.subprocess, "run", fake_run)
    backup_database._postgresql_snapshot(
        {
            "HOST": "127.0.0.1",
            "PORT": "5432",
            "USER": "operationalinbox_app",
            "PASSWORD": "do-not-log",
            "NAME": "operationalinbox",
        },
        destination,
    )
    assert len(calls) == 2
    assert all("do-not-log" not in " ".join(command) for command, _ in calls)
    assert calls[0][1]["env"]["PGPASSWORD"] == "do-not-log"
    assert calls[1][0][1] == "--list"


def test_postgresql_backup_failure_is_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_database.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        backup_database.subprocess,
        "run",
        lambda *args, **kwargs: Mock(returncode=1, stdout="", stderr="password=secret"),
    )
    with pytest.raises(CommandError, match="output was suppressed") as error:
        backup_database._postgresql_snapshot(
            {
                "HOST": "127.0.0.1",
                "PORT": "5432",
                "USER": "app",
                "PASSWORD": "secret",
                "NAME": "database",
            },
            tmp_path / "database.pgdump",
        )
    assert "secret" not in str(error.value)


@pytest.mark.django_db
def test_database_manifest_is_deterministic(tmp_path):
    manifest = build_database_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    call_command("validate_database_manifest", manifest_path, verbosity=0)


@pytest.mark.django_db
def test_postgresql_only_checks_fail_closed_on_sqlite():
    with pytest.raises(CommandError, match="not PostgreSQL"):
        call_command("validate_postgresql_sequences", verbosity=0)
    with pytest.raises(CommandError, match="not PostgreSQL"):
        call_command("check_postgresql_concurrency", verbosity=0)
