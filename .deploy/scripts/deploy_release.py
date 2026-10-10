"""Run the existing Fabric release as one serialized remote operation.

Only the deployer uploads this script and its mode-0600 input. Application code,
the runtime environment, and dependencies are changed inside the deployment lock.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import secrets
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

JOB_LOCKS = ("ingest", "scheduler", "dns", "retention", "backup")
ORM_SMOKE = """
from django.apps import apps
checked = 0
for model in apps.get_models():
    if model._meta.managed and not model._meta.proxy:
        fields = [field.attname for field in model._meta.concrete_fields]
        list(model._default_manager.values_list(*fields)[:1])
        checked += 1
print(f"models_checked={checked}")
""".strip()


class DeployError(RuntimeError):
    pass


def env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            candidate, raw = line.split("=", 1)
            if candidate.strip() == key:
                values = shlex.split(raw, comments=True)
                return values[0] if values else ""
    return ""


def merge_environment(text: str, updates: dict[str, str]) -> str:
    """Keep unchanged values and formatting; never rewrite an unchanged .env."""
    changed = {key: value for key, value in updates.items() if env_value(text, key) != value}
    if not changed:
        return text
    lines = []
    applied = set()
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in changed:
            lines.append(f"{key}={shlex.quote(changed[key])}")
            applied.add(key)
        else:
            lines.append(line)
    lines.extend(
        f"{key}={shlex.quote(value)}" for key, value in changed.items() if key not in applied
    )
    return "\n".join(lines).rstrip("\n") + "\n"


def atomic_write(path: Path, content: str, *, mode: int = 0o600, owner=None) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), mode)
        if owner:
            os.fchown(handle.fileno(), owner.pw_uid, owner.pw_gid)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migration_changed(paths: list[str]) -> bool:
    return any("/migrations/" in path or path.endswith(".sql") for path in paths)


def process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None


class Release:
    def __init__(self, config: dict):
        self.config = config
        self.project = Path(config["project_dir"])
        self.python = Path(config["venv_dir"]) / "bin/python"
        self.account = pwd.getpwnam(config["app_user"])
        self.directory = Path(config["state_dir"])
        self.marker = self.directory / "active.json"
        self.blocked = self.directory / "blocked"
        self.cron = Path(config["cron_path"])
        self.paused_cron = self.directory / "paused.cron"
        self.environment = self.project / ".env"
        self.unit = Path(config["mcp_systemd_path"])
        self.locks: list[int] = []
        self.state: dict = {}
        self.log = self.directory / "release.log"
        name = config["project_name"]
        self.units = [f"app@{name}.socket", f"app@{name}.service", config["mcp_service"]]
        systemd = Path(config.get("systemd_dir", "/etc/systemd/system"))
        self.guards = [systemd / (unit + ".d") / "deploy-guard.conf" for unit in self.units]
        self.guard_text = f"[Unit]\nConditionPathExists=!{self.blocked}\n"

    def notice(self, message: str) -> None:
        with contextlib.suppress(BrokenPipeError):
            print(message, flush=True)

    def acquire(self, suffix: str) -> None:
        path = Path(self.config.get("lock_dir", "/run/lock")) / (
            self.config["project_name"] + f"-{suffix}.lock"
        )
        # O_CREAT on an existing Ubuntu-owned /run/lock file fails with protected_regular.
        try:
            descriptor = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o664)
            except FileExistsError:
                descriptor = os.open(path, os.O_RDWR)
            else:
                os.fchown(descriptor, self.account.pw_uid, self.account.pw_gid)
        deadline = time.monotonic() + self.config.get("lock_timeout", 120)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locks.append(descriptor)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DeployError(f"Timed out acquiring the {suffix} lock.") from None
                    time.sleep(0.25)
        except BaseException:
            os.close(descriptor)
            raise

    def run(self, label: str, command: list[str], *, app: bool = False, timeout=600) -> str:
        identity = (
            {
                "user": self.account.pw_uid,
                "group": self.account.pw_gid,
                "extra_groups": os.getgrouplist(self.account.pw_name, self.account.pw_gid),
            }
            if app
            else {}
        )
        environment = os.environ.copy()
        if app:
            environment["HOME"] = self.account.pw_dir
        if command[0] == "git":
            environment["GIT_TERMINAL_PROMPT"] = "0"
        # Children retain the locks if the SSH parent disappears during a command.
        process = subprocess.Popen(  # noqa: S603 - fixed contract commands, no shell expansion.
            command,
            cwd=self.project if app else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            pass_fds=tuple(self.locks),
            env=environment,
            **identity,
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        with self.log.open("a") as handle:
            handle.write(f"\n[{label}] exit={process.returncode}\n{output}\n")
        if process.returncode:
            raise DeployError(f"{label} failed; private diagnostic log: {self.log}")
        return output.strip()

    def git(self, *arguments: str) -> str:
        return self.run("Git", ["git", "-C", str(self.project), *arguments], app=True)

    def manage(self, *arguments: str) -> str:
        return self.run(
            "Django " + arguments[0], [str(self.python), "manage.py", *arguments], app=True
        )

    def phase(self, phase: str, **changes) -> None:
        self.state.update(phase=phase, **changes)
        atomic_write(self.marker, json.dumps(self.state, indent=2) + "\n")
        self.notice(f"release phase={phase}")

    def schema_ready(self) -> None:
        self.manage("makemigrations", "--check", "--dry-run")
        self.manage("migrate", "--check")
        self.notice(self.manage("shell", "--no-imports", "-c", ORM_SMOKE))

    def pending(self) -> bool:
        if not self.python.is_file():
            return True
        try:
            self.manage("migrate", "--check")
        except DeployError:
            return True
        return False

    def owner_active(self, state: dict) -> bool:
        start = process_start(state["pid"])
        return start is not None and start == state["pid_start"]

    def preflight(self) -> dict:
        if self.marker.exists():
            old_state = json.loads(self.marker.read_text())
            if not self.config.get("recover") or self.owner_active(old_state):
                raise DeployError(
                    "An unfinished release is recorded; inspect it and use deploy --recover."
                )
            self.notice(f"Recovering contained release phase={old_state['phase']}")
            return old_state
        if self.paused_cron.exists() or self.blocked.exists():
            raise DeployError("Release containment has no marker; inspection is required.")
        return {}

    def prepare_source(self) -> str | None:
        self.project.mkdir(parents=True, exist_ok=True)
        os.chown(self.project, self.account.pw_uid, self.account.pw_gid)
        previous = None
        if (self.project / ".git").is_dir():
            if self.git("status", "--porcelain", "--untracked-files=normal"):
                raise DeployError("The production checkout has local changes.")
            previous = self.git("rev-parse", "HEAD")
            if self.git("branch", "--show-current") != "main":
                raise DeployError("The production checkout must remain on main.")
            if self.git("remote", "get-url", "origin") != self.config["repo_url"]:
                raise DeployError("The production Git remote differs from the deployment contract.")
        elif any(self.project.iterdir()):
            raise DeployError("The production directory is not an empty Git checkout.")
        else:
            self.run("Git init", ["git", "init", "-b", "main", str(self.project)], app=True)
            self.git("remote", "add", "origin", self.config["repo_url"])
        auth = base64.b64encode(f"x-access-token:{self.config['github_token']}".encode()).decode()
        self.git(
            "-c",
            "credential.helper=",
            "-c",
            f"http.extraHeader=Authorization: Basic {auth}",
            "fetch",
            "origin",
            "main",
            "--prune",
        )
        if self.git("rev-parse", "origin/main") != self.config["revision"]:
            raise DeployError("origin/main changed; the pinned local release is no longer its tip.")
        return previous

    def plan(self, previous: str | None, recovery: dict) -> dict:
        paths = (
            self.git("diff", "--name-only", previous, self.config["revision"]).splitlines()
            if previous
            else []
        )
        schema = bool(recovery) or not previous or migration_changed(paths) or self.pending()
        text = self.environment.read_text() if self.environment.exists() else ""
        if not text:
            defaults = self.config["initial_environment"] | {
                "DJANGO_SECRET_KEY": secrets.token_hex(48)
            }
            text = "".join(f"{key}={shlex.quote(value)}\n" for key, value in defaults.items())
        environment = merge_environment(text, self.config["environment_updates"])
        if env_value(environment, "DJANGO_EMAIL_BACKEND") != "ses":
            raise DeployError("The production email backend must be ses.")
        requirements = self.git("show", self.config["revision"] + ":requirements.txt")
        requirement_hash = hashlib.sha256(requirements.encode()).hexdigest()
        stamp = self.directory / "requirements.sha256"
        dependencies = (
            not self.python.is_file()
            or not stamp.is_file()
            or stamp.read_text().strip() != requirement_hash
        )
        unit = not self.unit.is_file() or self.unit.read_text() != self.config["mcp_unit"]
        guards = any(
            not path.is_file() or path.read_text() != self.guard_text for path in self.guards
        )
        env_changed = not self.environment.is_file() or self.environment.read_text() != environment
        static = (
            dependencies
            or not (self.project / "staticfiles").is_dir()
            or any("/static/" in "/" + path or path.startswith("static/") for path in paths)
        )
        return {
            "schema": schema,
            "quiesce": schema or dependencies or unit or env_changed or guards,
            "dependencies": dependencies,
            "static": static,
            "unit": unit,
            "guards": guards,
            "environment": environment,
            "env_changed": env_changed,
            "requirement_hash": requirement_hash,
        }

    def pause(self) -> None:
        atomic_write(self.blocked, "Workloads must remain stopped until schema verification.\n")
        if self.cron.exists():
            if self.paused_cron.exists():
                raise DeployError("Both the live and paused cron files exist.")
            self.cron.replace(self.paused_cron)
        elif not self.paused_cron.exists():
            raise DeployError("The StageOps cron contract is missing.")
        for suffix in JOB_LOCKS:
            self.acquire(suffix)
        self.run("Stop runtimes", ["systemctl", "stop", *self.units])
        for unit in self.units:
            state = self.run(
                "Verify stopped runtime",
                ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            )
            if state not in {"inactive", "failed"}:
                raise DeployError(f"Runtime {unit} is still active.")

    def activate(self) -> None:
        self.run("Reset runtimes", ["systemctl", "reset-failed", *self.units])
        self.run("Start web socket", ["systemctl", "restart", self.units[0]])
        self.run("Start MCP", ["systemctl", "restart", self.units[2]])

    def verify(self, revision: str) -> None:
        if self.git("rev-parse", "HEAD") != revision:
            raise DeployError("The deployed revision does not match the intended release.")
        self.schema_ready()
        deadline = time.monotonic() + 45
        consecutive = 0
        while consecutive < 3:
            try:
                for path, status in (("/health/live", "live"), ("/health/ready", "ready")):
                    with urllib.request.urlopen(
                        "https://" + self.config["domain"] + path, timeout=5
                    ) as response:
                        if response.status != 200 or json.load(response) != {"status": status}:
                            raise DeployError("Unexpected web health response.")
                self.verify_mcp()
            except (OSError, ValueError, DeployError):
                consecutive = 0
                if time.monotonic() >= deadline:
                    raise DeployError(
                        "Live web/MCP checks did not pass within 45 seconds."
                    ) from None
            else:
                consecutive += 1
            time.sleep(2)
        self.run("Verify runtime services", ["systemctl", "is-active", *self.units])
        self.run("Verify cron daemon", ["systemctl", "is-active", "cron.service"])

    def verify_mcp(self) -> None:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "deploy-check",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "deploy-check", "version": "1.0"},
                },
            }
        ).encode()
        request = urllib.request.Request(
            "http://127.0.0.1:8012/mcp",
            data=payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3):  # noqa: S310 - fixed loopback HTTP URL.
                raise DeployError("Unauthenticated MCP initialize must return 401.")
        except urllib.error.HTTPError as exc:
            challenge = exc.headers.get("WWW-Authenticate", "")
            if (
                exc.code != 401
                or not challenge.startswith("Bearer ")
                or "resource_metadata=" not in challenge
            ):
                raise DeployError("The MCP authentication challenge is invalid.") from None

    def restore_cron(self) -> None:
        if self.paused_cron.exists():
            if self.cron.exists():
                raise DeployError("Refusing to overwrite a newly created cron file.")
            self.paused_cron.replace(self.cron)
        if not self.cron.is_file() or self.cron.stat().st_mode & 0o022:
            raise DeployError("The StageOps cron file is missing or has unsafe permissions.")

    def restore_previous(self) -> None:
        previous = self.state.get("previous")
        if not previous:
            raise DeployError("There is no previous release to restore.")
        if self.state.get("mutated"):
            self.run("Stop runtimes", ["systemctl", "stop", *self.units])
            self.git("reset", "--hard", previous)
            for source, target, mode, owner in (
                (self.directory / "previous.env", self.environment, 0o600, self.account),
                (self.directory / "previous.unit", self.unit, 0o644, None),
            ):
                if source.exists():
                    atomic_write(target, source.read_text(), mode=mode, owner=owner)
            if self.state.get("dependencies"):
                self.run(
                    "Restore dependencies",
                    [str(self.python), "-m", "pip", "install", "-r", "requirements.txt"],
                    app=True,
                )
                self.run("Check dependencies", [str(self.python), "-m", "pip", "check"], app=True)
            if self.state.get("static"):
                self.manage("collectstatic", "--noinput")
            self.run("Reload previous units", ["systemctl", "daemon-reload"])
        self.schema_ready()
        self.blocked.unlink(missing_ok=True)
        self.activate()
        self.verify(previous)
        self.restore_cron()
        self.marker.unlink()
        self.notice(f"Previous release restored revision={previous}")

    def execute(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.log.touch(mode=0o600, exist_ok=True)
        self.log.chmod(0o600)
        self.acquire("deploy")
        try:
            recovery = self.preflight()
            previous = self.prepare_source()
            plan = self.plan(previous, recovery)
            self.state = recovery or {
                "previous": previous,
                "schema_started": False,
                "mutated": False,
            }
            self.state.update(
                pid=os.getpid(),
                pid_start=process_start(os.getpid()),
                revision=self.config["revision"],
                dependencies=plan["dependencies"],
                static=plan["static"],
            )
            if not recovery:
                for source, destination in (
                    (self.environment, "previous.env"),
                    (self.unit, "previous.unit"),
                ):
                    saved = self.directory / destination
                    if source.exists():
                        atomic_write(saved, source.read_text())
                    else:
                        saved.unlink(missing_ok=True)
            self.phase("prepared", schema=plan["schema"])
            try:
                if plan["quiesce"]:
                    self.phase("quiescing")
                    if plan["guards"]:
                        for path in self.guards:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write(path, self.guard_text, mode=0o644)
                        self.run("Reload containment guards", ["systemctl", "daemon-reload"])
                    self.pause()
                if plan["schema"] and previous:
                    self.phase("backup")
                    self.manage("backup_database")
                self.phase("checkout", mutated=True)
                self.git("reset", "--hard", self.config["revision"])
                self.run(
                    "Verify Python",
                    ["python3", "-c", "import sys; assert sys.version_info[:2] == (3, 12)"],
                )
                backup = Path(self.config["backup_dir"])
                backup.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chown(backup, self.account.pw_uid, self.account.pw_gid)
                if plan["env_changed"]:
                    atomic_write(self.environment, plan["environment"], owner=self.account)
                if plan["dependencies"]:
                    self.phase("dependencies")
                    if not self.python.is_file():
                        self.run(
                            "Create venv",
                            ["python3", "-m", "venv", self.config["venv_dir"]],
                            app=True,
                        )
                    self.run(
                        "Install dependencies",
                        [str(self.python), "-m", "pip", "install", "-r", "requirements.txt"],
                        app=True,
                    )
                self.run("Check dependencies", [str(self.python), "-m", "pip", "check"], app=True)
                self.manage("makemigrations", "--check", "--dry-run")
                if plan["schema"]:
                    self.notice(self.manage("migrate", "--plan"))
                    self.phase("migrating", schema_started=True)
                    self.manage("migrate", "--noinput")
                self.schema_ready()
                if plan["static"]:
                    self.phase("static")
                    self.manage("collectstatic", "--noinput")
                self.manage("check", "--deploy")
                if plan["unit"]:
                    atomic_write(self.unit, self.config["mcp_unit"], mode=0o644)
                    self.run("Reload MCP unit", ["systemctl", "daemon-reload"])
                    self.run("Enable MCP unit", ["systemctl", "enable", self.units[2]])
                self.phase("opening")
                self.blocked.unlink(missing_ok=True)
                self.activate()
                self.verify(self.config["revision"])
                self.phase("verified")
                self.restore_cron()
                atomic_write(
                    self.directory / "requirements.sha256", plan["requirement_hash"] + "\n"
                )
                self.phase("complete")
                atomic_write(self.directory / "last-success.json", self.marker.read_text())
                self.marker.unlink()
                self.notice(
                    f"Deployment verified revision={self.config['revision']} "
                    f"schema_changing={plan['schema']}"
                )
            except BaseException:
                if not self.state.get("schema_started"):
                    try:
                        self.restore_previous()
                    except BaseException:
                        self.contain()
                else:
                    self.contain()
                raise
        finally:
            for descriptor in reversed(self.locks):
                os.close(descriptor)
            self.locks.clear()

    def contain(self) -> None:
        atomic_write(self.blocked, "Contained failed release; roll-forward is required.\n")
        self.phase("failed")
        if self.cron.exists() and not self.paused_cron.exists():
            self.cron.replace(self.paused_cron)
        self.run("Contain failed release", ["systemctl", "stop", *self.units])
        self.notice(
            "Release contained: web, MCP and application cron remain stopped; "
            "roll-forward is required."
        )


def main() -> None:
    input_path = Path(sys.argv[1])
    try:
        config = json.loads(input_path.read_text())
    finally:
        input_path.unlink(missing_ok=True)

    def interrupted(_signum, _frame):
        raise DeployError("Deployment interrupted.")

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        Release(config).execute()
    except Exception as exc:
        # Exception messages must not contain subprocess arguments or secret values.
        message = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        print(f"Deployment failed: {message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
