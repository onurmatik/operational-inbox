from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
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


load_env(DEPLOY_DIR / ".credentials.env")
load_env(DEPLOY_DIR / "deploy.env")
load_env(DEPLOY_DIR.parent / ".env-prod")

PROJECT_NAME = os.environ.get("PROJECT_NAME", "operationalinbox")
DOMAIN = os.environ.get("DOMAIN", "operationalinbox.com")
GITHUB_APP_REPO = os.environ.get("GITHUB_APP_REPO", "onurmatik/operational-inbox")
DEPLOY_HOST = os.environ.get("DEPLOY_HOST", "46.225.14.95")
KEY_FILENAME = os.environ.get("KEY_FILENAME", "hetzner-stage")
DEPLOY_USER = os.environ.get("DEPLOY_USER", "root")
APP_USER = os.environ.get("APP_USER", "ubuntu")
MCP_SERVICE = f"{PROJECT_NAME}-mcp.service"
MCP_SYSTEMD_PATH = f"/etc/systemd/system/{MCP_SERVICE}"

PROJECT_DIR = f"/srv/apps/{PROJECT_NAME}"
VENV_DIR = f"{PROJECT_DIR}/venv"
BACKUP_DIR = f"/var/backups/{PROJECT_NAME}"
REPO_URL = f"https://github.com/{GITHUB_APP_REPO}.git"
POSTGRESQL_ROLE = "operationalinbox_app"
POSTGRESQL_DATABASE = "operationalinbox"
POSTGRESQL_STAGED_ENV = f"{PROJECT_DIR}/.env.postgresql"
RUNTIME_ENV_KEYS = (
    "MAX_PROJECTS_PER_ORGANIZATION",
    "MAX_DOMAINS_PER_ORGANIZATION",
    "MAX_DOMAINS_PER_USER",
    "DOMAIN_PROVISION_RATE_LIMIT",
    "DOMAIN_PROVISION_RATE_WINDOW_SECONDS",
    "DOMAIN_CLAIM_TTL_HOURS",
    "SIGNUP_RATE_LIMIT",
    "SIGNUP_RATE_WINDOW_SECONDS",
    "VERIFICATION_RESEND_RATE_LIMIT",
    "VERIFICATION_RESEND_RATE_WINDOW_SECONDS",
    "INBOUND_SERVICE_DOMAIN",
    "PUBLIC_BASE_URL",
    "MCP_RESOURCE_URL",
    "MCP_DOCUMENTATION_URL",
    "MCP_ALLOWED_ORIGINS",
    "OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED",
    "OAUTH_ISSUER",
    "OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS",
    "OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS",
    "OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS",
    "OAUTH_DCR_ENABLED",
    "OAUTH_DCR_PER_IP_HOURLY_LIMIT",
    "OAUTH_DCR_GLOBAL_HOURLY_LIMIT",
    "OAUTH_DCR_MAX_REDIRECT_URIS",
    "OAUTH_DCR_CLIENT_RETENTION_DAYS",
    "OPENAI_APPS_CHALLENGE_TOKEN",
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
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRO_UNIT_AMOUNT",
    "STRIPE_PRO_CURRENCY",
    "BACKUP_ENCRYPTION_KEY",
)


def quote(value: str) -> str:
    return shlex.quote(value)


def get_github_token() -> str:
    """Mint a short-lived token locally and fail before touching the server."""

    helper = DEPLOY_DIR / "scripts" / "get_github_app_token.py"
    if not helper.is_file():
        raise RuntimeError("GitHub App token helper is missing.")
    try:
        result = subprocess.run(  # noqa: S603 - interpreter and helper are fixed local paths.
            [sys.executable, str(helper)],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub App token minting timed out.") from exc
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise RuntimeError(
            "GitHub App token minting failed. Check .deploy/.credentials.env "
            "and the app installation's repository access."
        )
    return token


def app_run(connection: Connection, command: str, *, warn: bool = False):
    snippet = f"cd {quote(PROJECT_DIR)} && {command}"
    return connection.sudo(
        f"bash -lc {quote(snippet)}",
        user=APP_USER,
        warn=warn,
    )


def production_connection() -> Connection:
    return Connection(
        host=DEPLOY_HOST,
        user=DEPLOY_USER,
        connect_kwargs={"key_filename": str(Path(f"~/.ssh/{KEY_FILENAME}").expanduser())},
    )


def rendered_mcp_unit() -> str:
    template = (DEPLOY_DIR / "systemd" / "operationalinbox-mcp.service").read_text()
    return (
        template.replace("__APP_USER__", APP_USER)
        .replace("__PROJECT_DIR__", PROJECT_DIR)
        .replace("__VENV_DIR__", VENV_DIR)
    )


def verify_mcp_runtime(connection: Connection) -> None:
    script = """
import json
import time
import urllib.error
import urllib.request

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": "deploy-check",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "deploy-check", "version": "1.0"},
    },
}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8012/mcp",
    data=payload,
    headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
)
deadline = time.monotonic() + 30
while True:
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP readiness deadline elapsed.")
        urllib.request.urlopen(request, timeout=min(2, remaining))
        raise AssertionError("Unauthenticated MCP initialize must return 401.")
    except urllib.error.HTTPError as exc:
        challenge = exc.headers.get("WWW-Authenticate", "")
        if (
            exc.code == 401
            and challenge.startswith("Bearer ")
            and "resource_metadata=" in challenge
        ):
            break
        last_error = exc
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        KeyError,
        AssertionError,
    ) as exc:
        last_error = exc
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("MCP did not become ready within 30 seconds.") from last_error
    time.sleep(min(1, remaining))
""".strip()
    app_run(connection, f"{quote(VENV_DIR + '/bin/python')} -c {quote(script)}")


def local_database_url() -> str:
    path = DEPLOY_DIR.parent / ".env-prod"
    if not path.is_file():
        raise RuntimeError("The ignored .env-prod file is missing.")
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() != "DJANGO_DATABASE_URL":
            continue
        parsed = shlex.split(raw_value, comments=True, posix=True)
        value = parsed[0] if parsed else ""
        if "\n" in value or "\r" in value:
            break
        if value.startswith(("postgresql://", "postgres://")):
            return value
        break
    raise RuntimeError(".env-prod does not contain a valid PostgreSQL DJANGO_DATABASE_URL.")


def stage_database_env(connection: Connection) -> None:
    payload = f"DJANGO_DATABASE_URL={quote(local_database_url())}\n"
    temporary_path = connection.run("mktemp", hide=True).stdout.strip()
    try:
        connection.put(BytesIO(payload.encode()), remote=temporary_path)
        connection.sudo(
            f"install -o {quote(APP_USER)} -g {quote(APP_USER)} -m 600 "
            f"{quote(temporary_path)} {quote(POSTGRESQL_STAGED_ENV)}"
        )
    finally:
        connection.run(f"rm -f {quote(temporary_path)}", warn=True, hide=True)


def remove_staged_database_env(connection: Connection) -> None:
    connection.sudo(f"rm -f {quote(POSTGRESQL_STAGED_ENV)}", warn=True, hide=True)


def remove_local_database_url() -> None:
    path = DEPLOY_DIR.parent / ".env-prod"
    lines = path.read_text().splitlines()
    retained = [
        line
        for line in lines
        if not (
            line.strip()
            and not line.lstrip().startswith("#")
            and line.split("=", 1)[0].strip() == "DJANGO_DATABASE_URL"
        )
    ]
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text("\n".join(retained).rstrip("\n") + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def postgresql_migration_command(action: str) -> str:
    script = f"{PROJECT_DIR}/.deploy/scripts/postgresql_migration.py"
    arguments = [
        "python3",
        script,
        action,
        "--project-dir",
        PROJECT_DIR,
        "--backup-dir",
        BACKUP_DIR,
        "--staged-env",
        POSTGRESQL_STAGED_ENV,
        "--runtime-env",
        f"{PROJECT_DIR}/.env",
        "--sqlite-path",
        f"{PROJECT_DIR}/db.sqlite3",
        "--venv-python",
        f"{VENV_DIR}/bin/python",
        "--project-name",
        PROJECT_NAME,
        "--app-user",
        APP_USER,
        "--mcp-service",
        MCP_SERVICE,
        "--database-role",
        POSTGRESQL_ROLE,
        "--database-name",
        POSTGRESQL_DATABASE,
    ]
    return " ".join(quote(value) for value in arguments)


def verify_web_runtime(connection: Connection) -> None:
    script = f"""
import json
import urllib.request

for path, expected in (
    ("/health/live", {{"status": "live"}}),
    ("/health/ready", {{"status": "ready"}}),
):
    with urllib.request.urlopen("https://{DOMAIN}" + path, timeout=10) as response:
        assert response.status == 200
        assert json.load(response) == expected
""".strip()
    app_run(connection, f"{quote(VENV_DIR + '/bin/python')} -c {quote(script)}")


@task
def prepare_postgresql(_context) -> None:
    """Prepare and verify the isolated PostgreSQL target without changing runtime .env."""
    connection = production_connection()
    stage_database_env(connection)
    try:
        connection.sudo(postgresql_migration_command("prepare"))
    finally:
        remove_staged_database_env(connection)


@task
def cutover_postgresql(_context) -> None:
    """Move the stopped production runtime from SQLite to the prepared PostgreSQL database."""
    connection = production_connection()
    stage_database_env(connection)
    try:
        result = connection.sudo(postgresql_migration_command("cutover"), warn=True)
        if result.exited == 20:
            remove_local_database_url()
            raise RuntimeError("PostgreSQL cutover failed and production was restored to SQLite.")
        if result.failed:
            raise RuntimeError(
                "PostgreSQL cutover requires roll-forward; production services remain stopped."
            )
        try:
            verify_web_runtime(connection)
            verify_mcp_runtime(connection)
            vendor_check = (
                "from django.db import connection; "
                "assert connection.vendor == 'postgresql'; print(connection.vendor)"
            )
            app_run(
                connection,
                f"{quote(VENV_DIR + '/bin/python')} manage.py shell --no-imports -c "
                f"{quote(vendor_check)}",
            )
        except Exception as exc:
            connection.sudo(
                f"systemctl stop app@{PROJECT_NAME}.socket app@{PROJECT_NAME}.service "
                f"{quote(MCP_SERVICE)}",
                warn=True,
            )
            raise RuntimeError(
                "PostgreSQL opened but live verification failed; roll-forward is required."
            ) from exc
    finally:
        remove_staged_database_env(connection)


def local_release_revision() -> str:
    root = DEPLOY_DIR.parent
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required for the local release check.")

    def local_git(*arguments: str) -> str:
        return subprocess.check_output(  # noqa: S603 - resolved Git, fixed read-only arguments.
            [git, *arguments], cwd=root, text=True
        ).strip()

    if local_git("status", "--porcelain"):
        raise RuntimeError("Commit and publish the intended release before deploying.")
    revision = local_git("rev-parse", "HEAD")
    upstream = local_git("rev-parse", "origin/main")
    if revision != upstream:
        raise RuntimeError(
            "The local release differs from origin/main; publish it before deploying."
        )
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("The local project .venv is required for the read-only schema check.")
    result = subprocess.run(  # noqa: S603 - fixed project interpreter and read-only command.
        [str(python), "manage.py", "makemigrations", "--check", "--dry-run"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            "The local release failed the read-only model/migration consistency check."
        )
    return revision


@task
def deploy(_context, recover=False) -> None:
    """Deploy the pinned origin/main release; recover resumes a contained failure."""
    revision = local_release_revision()
    updates = {
        key: os.environ[key].strip() for key in RUNTIME_ENV_KEYS if os.environ.get(key, "").strip()
    }
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise RuntimeError("Runtime environment values must be single-line strings.")
    config = {
        "revision": revision,
        "github_token": get_github_token(),
        "repo_url": REPO_URL,
        "project_name": PROJECT_NAME,
        "project_dir": PROJECT_DIR,
        "venv_dir": VENV_DIR,
        "backup_dir": BACKUP_DIR,
        "app_user": APP_USER,
        "domain": DOMAIN,
        "mcp_service": MCP_SERVICE,
        "mcp_systemd_path": MCP_SYSTEMD_PATH,
        "mcp_unit": rendered_mcp_unit(),
        "recover": bool(recover),
        "state_dir": f"/var/lib/{PROJECT_NAME}-deploy",
        "cron_path": f"/etc/cron.d/stageops-{PROJECT_NAME}",
        "environment_updates": updates,
        "initial_environment": {
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": DOMAIN,
            "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{DOMAIN}",
            "DJANGO_SECURE_COOKIES": "true",
            "DJANGO_SECURE_SSL_REDIRECT": "true",
            "DJANGO_DATABASE_URL": "sqlite:///db.sqlite3",
            "DJANGO_EMAIL_BACKEND": "ses",
            "TRUSTED_PROXY_IPS": "127.0.0.1,::1",
            "PUBLIC_BASE_URL": f"https://{DOMAIN}",
            "INBOUND_SERVICE_DOMAIN": f"inbound.{DOMAIN}",
            "BACKUP_DIRECTORY": BACKUP_DIR,
        },
    }
    connection = production_connection()
    staging = connection.run("mktemp -d", hide=True).stdout.strip()
    try:
        script = f"{staging}/deploy_release.py"
        payload = f"{staging}/release.json"
        connection.put(str(DEPLOY_DIR / "scripts" / "deploy_release.py"), remote=script)
        connection.put(BytesIO(json.dumps(config).encode()), remote=payload)
        connection.run(f"chmod 600 {quote(script)} {quote(payload)}", hide=True)
        result = connection.sudo(f"python3 {quote(script)} {quote(payload)}", warn=True)
        if result.failed:
            raise RuntimeError(
                "Deployment failed. Inspect the recorded release state and runtime health; "
                "a contained schema failure requires roll-forward with deploy --recover."
            )
    finally:
        connection.run(f"rm -rf -- {quote(staging)}", warn=True, hide=True)
        connection.close()


ns = Collection(deploy, prepare_postgresql, cutover_postgresql)
