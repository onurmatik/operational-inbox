from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pwd
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".deploy/scripts/deploy_release.py"
SPEC = importlib.util.spec_from_file_location("deploy_release", SCRIPT)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)

PREVIOUS = "a" * 40
CANDIDATE = "b" * 40
REQUIREMENTS = "django==5.2.16"


class FakeRelease(contract.Release):
    """Exercise real state files and locks without changing systemd or a database."""

    def __init__(self, config, *, paths=(), failure=None):
        super().__init__(config)
        self.events = []
        self.paths = list(paths)
        self.failure = failure
        self.head = PREVIOUS
        self.running = True
        self.pending_migrations = False

    def acquire(self, suffix):
        super().acquire(suffix)
        self.events.append(("lock", suffix))

    def run(self, label, command, *, app=False, timeout=600):
        assert self.locks, "Every remote operation must be serialized."
        self.events.append((label, tuple(command)))
        if self.failure == label:
            self.failure = None
            raise contract.DeployError("Injected failure: " + label)
        if label == "Git":
            arguments = command[3:]
            if arguments[:1] == ["rev-parse"]:
                return CANDIDATE if arguments[1] == "origin/main" else self.head
            if arguments[:1] == ["branch"]:
                return "main"
            if arguments[:2] == ["remote", "get-url"]:
                return self.config["repo_url"]
            if arguments[:1] == ["diff"]:
                return "\n".join(self.paths)
            if arguments[:1] == ["show"]:
                return REQUIREMENTS
            if arguments[:1] == ["reset"]:
                self.head = arguments[-1]
            return ""
        if label == "Stop runtimes" or label == "Contain failed release":
            self.running = False
        if label == "Verify stopped runtime":
            return "active" if self.running else "inactive"
        if label == "Start MCP":
            self.running = True
        if command[1:2] == ["manage.py"]:
            arguments = command[2:]
            if arguments == ["migrate", "--check"] and self.pending_migrations:
                raise contract.DeployError("Pending migrations")
            if arguments == ["migrate", "--noinput"]:
                assert not self.running
                assert self.paused_cron.exists() and not self.cron.exists()
                assert len(self.locks) == 6
                if self.failure == "migration":
                    raise contract.DeployError("Injected migration failure")
                if self.failure == "interrupt":
                    raise KeyboardInterrupt
                self.pending_migrations = False
            if arguments[:1] == ["shell"]:
                return "models_checked=28"
        return ""

    def verify(self, revision):
        self.events.append(("verify", revision))
        assert self.head == revision
        assert self.running
        self.schema_ready()
        if self.failure == "health":
            raise contract.DeployError("Injected health failure")


@pytest.fixture
def config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "staticfiles").mkdir()
    (project / ".env").write_text("DJANGO_EMAIL_BACKEND=ses\nDEFAULT_FROM_EMAIL='Support'\n")
    (project / "venv/bin").mkdir(parents=True)
    (project / "venv/bin/python").touch()
    state = tmp_path / "state"
    state.mkdir()
    (state / "requirements.sha256").write_text(hashlib.sha256(REQUIREMENTS.encode()).hexdigest())
    locks = tmp_path / "locks"
    locks.mkdir()
    cron = tmp_path / "stageops-app"
    cron.write_text("* * * * * ubuntu flock -n app-ingest.lock ingest\n")
    cron.chmod(0o644)
    unit = tmp_path / "app-mcp.service"
    unit.write_text("[Service]\nExecStart=/app/venv/bin/python\n")
    systemd = tmp_path / "systemd"
    for name in ("app@app.socket", "app@app.service", "app-mcp.service"):
        guard = systemd / (name + ".d") / "deploy-guard.conf"
        guard.parent.mkdir(parents=True)
        guard.write_text(f"[Unit]\nConditionPathExists=!{state / 'blocked'}\n")
    return {
        "project_dir": str(project),
        "venv_dir": str(project / "venv"),
        "app_user": pwd.getpwuid(os.getuid()).pw_name,
        "project_name": "app",
        "state_dir": str(state),
        "lock_dir": str(locks),
        "lock_timeout": 0,
        "backup_dir": str(tmp_path / "backup"),
        "cron_path": str(cron),
        "mcp_service": "app-mcp.service",
        "mcp_systemd_path": str(unit),
        "mcp_unit": unit.read_text(),
        "systemd_dir": str(systemd),
        "revision": CANDIDATE,
        "repo_url": "https://example.test/app.git",
        "github_token": "test-installation-token",
        "environment_updates": {},
        "initial_environment": {},
        "domain": "example.test",
    }


def migration_release(config, **kwargs):
    return FakeRelease(config, paths=["inbox/migrations/0015_simplify_agent_first.py"], **kwargs)


def test_schema_release_quiesces_before_checkout_and_keeps_locks_through_verification(config):
    release = migration_release(config)
    release.execute()
    events = release.events
    checkout = next(
        i
        for i, event in enumerate(events)
        if event
        == (
            "Git",
            (
                "git",
                "-C",
                config["project_dir"],
                "reset",
                "--hard",
                CANDIDATE,
            ),
        )
    )
    stop = next(i for i, event in enumerate(events) if event[0] == "Stop runtimes")
    assert stop < checkout
    assert all(events.index(("lock", suffix)) < checkout for suffix in contract.JOB_LOCKS)
    assert any(event[0] == "Django backup_database" for event in events[:checkout])
    assert release.head == CANDIDATE and release.running
    assert release.cron.exists() and not release.paused_cron.exists()
    assert not release.marker.exists() and not release.locks
    state = json.loads((release.directory / "last-success.json").read_text())
    assert state["phase"] == "complete" and state["schema_started"]


@pytest.mark.parametrize("failure", ["migration", "interrupt", "health"])
def test_schema_failure_is_persistently_contained_after_lock_release(config, failure):
    release = migration_release(config, failure=failure)
    expected = KeyboardInterrupt if failure == "interrupt" else contract.DeployError
    with pytest.raises(expected):
        release.execute()
    assert release.head == CANDIDATE
    assert not release.running
    assert release.blocked.exists()
    assert all(release.blocked.name in guard.read_text() for guard in release.guards)
    assert release.paused_cron.exists() and not release.cron.exists()
    state = json.loads(release.marker.read_text())
    assert state["phase"] == "failed" and state["schema_started"]
    assert not release.locks
    if failure != "health":
        assert not any(event[0] == "Start MCP" for event in release.events)
    subsequent = migration_release(config)
    with pytest.raises(contract.DeployError, match="unfinished release"):
        subsequent.execute()
    assert not any(event[0] == "Git" for event in subsequent.events)
    assert subsequent.marker.exists()


@pytest.mark.parametrize("failure", ["Django backup_database", "Install dependencies"])
def test_failure_before_migration_restores_verified_previous_runtime(config, failure):
    (Path(config["state_dir"]) / "requirements.sha256").unlink()
    config["environment_updates"] = {"DEFAULT_FROM_EMAIL": "New sender"}
    release = migration_release(config, failure=failure)
    original = release.environment.read_text()
    with pytest.raises(contract.DeployError, match="Injected failure"):
        release.execute()
    assert release.head == PREVIOUS and release.running
    assert release.environment.read_text() == original
    assert release.cron.exists() and not release.paused_cron.exists()
    assert not release.marker.exists()
    assert not release.blocked.exists()
    assert ("verify", PREVIOUS) in release.events


def test_migrationless_release_skips_backup_shutdown_and_unchanged_installs(config):
    release = FakeRelease(config, paths=["inbox/views.py", "templates/inbox/conversations.html"])
    release.execute()
    labels = [event[0] for event in release.events]
    assert "Stop runtimes" not in labels
    assert "Django backup_database" not in labels
    assert "Install dependencies" not in labels
    assert "Django collectstatic" not in labels
    assert "Reload MCP unit" not in labels
    assert "Django migrate" in labels  # migrate --check still runs.
    assert not json.loads((release.directory / "last-success.json").read_text())["schema"]


def test_migrationless_health_failure_restores_previous_checkout(config):
    release = FakeRelease(config, failure="health")
    # Fail only the candidate's health check; the restored release must verify too.
    verify = release.verify

    def health_once(revision):
        try:
            verify(revision)
        finally:
            release.failure = None

    release.verify = health_once
    with pytest.raises(contract.DeployError, match="health failure"):
        release.execute()
    assert release.head == PREVIOUS and release.running
    assert not release.marker.exists()
    assert release.cron.exists()


def test_shared_dependency_change_quiesces_even_without_migrations(config):
    (Path(config["state_dir"]) / "requirements.sha256").unlink()
    release = FakeRelease(config, paths=["requirements.txt"])
    release.execute()
    labels = [event[0] for event in release.events]
    assert labels.index("Stop runtimes") < labels.index("Install dependencies")
    assert "Django backup_database" not in labels
    assert "Django collectstatic" in labels


def test_pending_target_migration_forces_quiescence(config):
    release = FakeRelease(config, paths=["README.md"])
    release.pending_migrations = True
    release.execute()
    assert json.loads((release.directory / "last-success.json").read_text())["schema"]
    assert any(event[0] == "Stop runtimes" for event in release.events)


def test_concurrent_release_cannot_fetch_or_modify_checkout(config):
    owner = FakeRelease(config)
    owner.acquire("deploy")
    try:
        contender = FakeRelease(config)
        with pytest.raises(contract.DeployError, match="deploy lock"):
            contender.execute()
        assert not contender.events
        assert contender.head == PREVIOUS and contender.cron.exists()
    finally:
        for descriptor in owner.locks:
            os.close(descriptor)


def test_recovery_retains_containment_until_new_revision_verifies(config, monkeypatch):
    failed = migration_release(config, failure="migration")
    with pytest.raises(contract.DeployError):
        failed.execute()
    config["recover"] = True
    monkeypatch.setattr(contract, "process_start", lambda _pid: None)
    recovery = migration_release(config)
    recovery.head = CANDIDATE
    recovery.running = False
    recovery.execute()
    assert recovery.head == CANDIDATE and recovery.running
    assert recovery.cron.exists() and not recovery.marker.exists()
    assert not recovery.blocked.exists()
    assert ("verify", CANDIDATE) in recovery.events
    assert ("verify", PREVIOUS) not in recovery.events


def test_environment_merge_preserves_unchanged_formatting_and_quotes_updates():
    original = "# runtime\nDEFAULT_FROM_EMAIL='Support Team'\nKEEP=untouched\n"
    assert contract.merge_environment(original, {"DEFAULT_FROM_EMAIL": "Support Team"}) == original
    updated = contract.merge_environment(original, {"DEFAULT_FROM_EMAIL": "New Team", "NEW": "a b"})
    assert contract.env_value(updated, "DEFAULT_FROM_EMAIL") == "New Team"
    assert contract.env_value(updated, "NEW") == "a b"
    assert "KEEP=untouched" in updated


def test_live_verification_requires_repeated_web_health_and_bearer_challenge(config, monkeypatch):
    release = FakeRelease(config)
    release.acquire("deploy")
    release.head = CANDIDATE
    urls = []

    def urlopen(request, **_kwargs):
        url = request if isinstance(request, str) else request.full_url
        urls.append(url)
        if url.endswith("/mcp"):
            raise urllib.error.HTTPError(
                url,
                401,
                "Unauthorized",
                {
                    "WWW-Authenticate": 'Bearer resource_metadata="https://example.test/metadata"',
                },
                None,
            )
        response = io.BytesIO(json.dumps({"status": url.rsplit("/", 1)[1]}).encode())
        response.status = 200
        return response

    monkeypatch.setattr(contract.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(contract.time, "sleep", lambda _seconds: None)
    try:
        contract.Release.verify(release, CANDIDATE)
        assert urls.count("https://example.test/health/live") == 3
        assert urls.count("https://example.test/health/ready") == 3
        assert urls.count("http://127.0.0.1:8012/mcp") == 3
        assert any(event[0] == "Verify cron daemon" for event in release.events)
    finally:
        for descriptor in release.locks:
            os.close(descriptor)
