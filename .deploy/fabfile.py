from __future__ import annotations

import os
import shlex
from io import BytesIO
from pathlib import Path

from fabric import Connection, task
from invoke import Collection

DEPLOY_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        try:
            parsed = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise RuntimeError(f"Invalid shell quoting for {key} in {path.name}.") from exc
        value = parsed[0] if parsed else ""
        os.environ.setdefault(key, value)


load_env(DEPLOY_DIR / "deploy.env")
load_env(DEPLOY_DIR.parent / ".env-prod")

PROJECT_NAME = os.environ.get("PROJECT_NAME", "operationalinbox")
DOMAIN = os.environ.get("DOMAIN", "operationalinbox.com")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "onurmatik/operational-inbox")
DEPLOY_HOST = os.environ.get("DEPLOY_HOST", "46.225.14.95")
KEY_FILENAME = os.environ.get("KEY_FILENAME", "hetzner-stage")
DEPLOY_USER = os.environ.get("DEPLOY_USER", "root")
APP_USER = os.environ.get("APP_USER", "ubuntu")

PROJECT_DIR = f"/srv/apps/{PROJECT_NAME}"
VENV_DIR = f"{PROJECT_DIR}/venv"
BACKUP_DIR = f"/var/backups/{PROJECT_NAME}"
REPO_URL = f"git@github.com:{GITHUB_REPO}.git"
RUNTIME_ENV_KEYS = (
    "MAX_PROJECTS_PER_ORGANIZATION",
    "MAX_DOMAINS_PER_ORGANIZATION",
    "DOMAIN_PROVISION_RATE_LIMIT",
    "DOMAIN_PROVISION_RATE_WINDOW_SECONDS",
    "DOMAIN_CLAIM_TTL_HOURS",
    "SIGNUP_RATE_LIMIT",
    "SIGNUP_RATE_WINDOW_SECONDS",
    "VERIFICATION_RESEND_RATE_LIMIT",
    "VERIFICATION_RESEND_RATE_WINDOW_SECONDS",
    "INBOUND_SERVICE_DOMAIN",
    "PUBLIC_BASE_URL",
    "DEFAULT_FROM_EMAIL",
    "AWS_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_INGRESS_BUCKET",
    "AWS_INGRESS_QUEUE_URL",
    "AWS_INBOUND_TOPIC_ARN",
    "AWS_DELIVERY_TOPIC_ARN",
    "AWS_SES_CONFIGURATION_SET",
    "AWS_SES_RECEIPT_RULE_SET",
    "AWS_SES_RECEIPT_RULE",
    "AWS_SES_SYSTEM_IDENTITY",
    "OPENAI_API_KEY",
    "OPENAI_TRIAGE_MODEL",
    "OPENAI_DRAFT_MODEL",
    "OPENAI_REPORT_MODEL",
    "BACKUP_ENCRYPTION_KEY",
)


def quote(value: str) -> str:
    return shlex.quote(value)


def app_run(connection: Connection, command: str, *, warn: bool = False):
    snippet = f"cd {quote(PROJECT_DIR)} && {command}"
    return connection.sudo(
        f"bash -lc {quote(snippet)}",
        user=APP_USER,
        warn=warn,
    )


def ensure_runtime_env(connection: Connection) -> None:
    env_path = f"{PROJECT_DIR}/.env"
    connection.sudo(
        f"install -d -o {quote(APP_USER)} -g {quote(APP_USER)} -m 0700 {quote(BACKUP_DIR)}"
    )
    if connection.run(f"test -f {quote(env_path)}", warn=True, hide=True).failed:
        script = f"""
umask 077
{{
  printf 'DJANGO_SECRET_KEY=%s\\n' "$(openssl rand -hex 48)"
  printf 'DJANGO_DEBUG=false\\n'
  printf 'DJANGO_ALLOWED_HOSTS={DOMAIN}\\n'
  printf 'DJANGO_CSRF_TRUSTED_ORIGINS=https://{DOMAIN}\\n'
  printf 'DJANGO_SECURE_COOKIES=true\\n'
  printf 'DJANGO_SECURE_SSL_REDIRECT=true\\n'
  printf 'DJANGO_DATABASE_URL=sqlite:///db.sqlite3\\n'
  printf 'DJANGO_EMAIL_BACKEND=ses\\n'
  printf 'TRUSTED_PROXY_IPS=127.0.0.1,::1\\n'
  printf 'PUBLIC_BASE_URL=https://{DOMAIN}\\n'
  printf 'INBOUND_SERVICE_DOMAIN=inbound.{DOMAIN}\\n'
  printf 'BACKUP_DIRECTORY={BACKUP_DIR}\\n'
}} > .env
chmod 600 .env
""".strip()
        app_run(connection, f"bash -lc {quote(script)}")
    else:
        app_run(connection, "chmod 600 .env")


def sync_runtime_env(connection: Connection) -> None:
    updates = {
        key: os.environ.get(key, "").strip()
        for key in RUNTIME_ENV_KEYS
        if os.environ.get(key, "").strip()
    }
    if not updates:
        return
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise RuntimeError("Runtime environment values must be single-line strings.")

    payload = "".join(f"{key}={quote(value)}\n" for key, value in updates.items())
    temporary_path = connection.run("mktemp", hide=True).stdout.strip()
    staged_path = f"{PROJECT_DIR}/.env.deploy"
    try:
        connection.put(BytesIO(payload.encode()), remote=temporary_path)
        connection.sudo(
            f"install -o {quote(APP_USER)} -g {quote(APP_USER)} -m 600 "
            f"{quote(temporary_path)} {quote(staged_path)}"
        )
        app_run(connection, "python3 .deploy/sync_env.py .env .env.deploy")
    finally:
        connection.run(f"rm -f {quote(temporary_path)}", warn=True, hide=True)
        app_run(connection, "rm -f .env.deploy", warn=True)


def deploy_under_locks(connection: Connection) -> None:
    python = quote(f"{VENV_DIR}/bin/python")
    command = f"""
exec 9>/run/lock/{PROJECT_NAME}-deploy.lock
flock -w 120 9
exec 8>/run/lock/{PROJECT_NAME}-ingest.lock
flock -w 120 8
exec 7>/run/lock/{PROJECT_NAME}-scheduler.lock
flock -w 120 7
exec 6>/run/lock/{PROJECT_NAME}-dns.lock
flock -w 120 6
exec 5>/run/lock/{PROJECT_NAME}-retention.lock
flock -w 120 5
exec 4>/run/lock/{PROJECT_NAME}-backup.lock
flock -w 120 4
if [ -f db.sqlite3 ]; then
  {python} manage.py backup_sqlite
fi
{python} manage.py migrate --noinput
{python} manage.py collectstatic --noinput
{python} manage.py check --deploy
""".strip()
    app_run(connection, f"bash -lc {quote(command)}")


@task
def deploy(_context) -> None:
    """Deploy the origin/main release to the StageOps Hetzner host."""
    connection = Connection(
        host=DEPLOY_HOST,
        user=DEPLOY_USER,
        forward_agent=True,
        connect_kwargs={"key_filename": str(Path(f"~/.ssh/{KEY_FILENAME}").expanduser())},
    )
    git_environment = {
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    }

    connection.run(f"mkdir -p {quote(PROJECT_DIR)}")
    connection.run(f"chown {quote(APP_USER)}:{quote(APP_USER)} {quote(PROJECT_DIR)}")
    if connection.run(f"test -d {quote(PROJECT_DIR + '/.git')}", warn=True, hide=True).ok:
        connection.run(
            f"git -c safe.directory={quote(PROJECT_DIR)} -C {quote(PROJECT_DIR)} "
            "fetch origin main --prune",
            env=git_environment,
        )
        connection.run(
            f"git -c safe.directory={quote(PROJECT_DIR)} -C {quote(PROJECT_DIR)} checkout main",
            env=git_environment,
        )
        connection.run(
            f"git -c safe.directory={quote(PROJECT_DIR)} -C {quote(PROJECT_DIR)} "
            "reset --hard origin/main",
            env=git_environment,
        )
    else:
        is_empty = connection.run(
            f'test -z "$(find {quote(PROJECT_DIR)} -mindepth 1 -maxdepth 1 -print -quit)"',
            warn=True,
            hide=True,
        ).ok
        if not is_empty:
            raise RuntimeError(f"{PROJECT_DIR} exists and is not an empty Git checkout")
        connection.run(f"git clone {quote(REPO_URL)} {quote(PROJECT_DIR)}", env=git_environment)
    connection.run(f"chown -R {quote(APP_USER)}:{quote(APP_USER)} {quote(PROJECT_DIR)}")

    ensure_runtime_env(connection)
    sync_runtime_env(connection)
    app_run(
        connection,
        "python3 -c "
        + quote(
            "import sys; "
            "assert sys.version_info[:2] == (3, 12), "
            "'Operational Inbox requires Python 3.12'"
        ),
    )
    if connection.run(f"test -x {quote(VENV_DIR + '/bin/python')}", warn=True, hide=True).failed:
        app_run(connection, f"python3 -m venv {quote(VENV_DIR)}")
    app_run(connection, f"{quote(VENV_DIR + '/bin/pip')} install --upgrade pip")
    app_run(connection, f"{quote(VENV_DIR + '/bin/pip')} install -r requirements.txt")

    connection.sudo(
        f"systemctl stop app@{PROJECT_NAME}.socket app@{PROJECT_NAME}.service",
        warn=True,
    )
    deploy_under_locks(connection)
    connection.sudo(
        f"systemctl reset-failed app@{PROJECT_NAME}.service app@{PROJECT_NAME}.socket",
        warn=True,
    )
    connection.sudo(f"systemctl restart app@{PROJECT_NAME}.socket")


ns = Collection(deploy)
