from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


class MigrationError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    user: str | None = None,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = False,
    stdout=None,
) -> str:
    child_environment = os.environ.copy()
    child_environment.update(environment or {})
    wrapped = command
    if user:
        wrapped = ["sudo"]
        if environment:
            wrapped.append("--preserve-env=" + ",".join(sorted(environment)))
        wrapped.extend(("-u", user, *command))
    result = subprocess.run(  # noqa: S603 - commands are fixed by the deployment contract.
        wrapped,
        env=child_environment,
        input=input_text,
        stdout=subprocess.PIPE if capture else stdout,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        executable = Path(command[0]).name
        raise MigrationError(f"{executable} failed; output suppressed to protect credentials.")
    return result.stdout.strip() if capture and result.stdout else ""


def read_env_value(path: Path, key: str) -> str:
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate, raw_value = line.split("=", 1)
        if candidate.strip() != key:
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        return values[0] if values else ""
    return ""


def parse_database_url(path: Path, expected_role: str, expected_database: str) -> dict[str, str]:
    value = read_env_value(path, "DJANGO_DATABASE_URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname != "127.0.0.1"
        or (parsed.port or 5432) != 5432
        or unquote(parsed.username or "") != expected_role
        or unquote(parsed.path.lstrip("/")) != expected_database
        or not parsed.password
    ):
        raise MigrationError(
            "The staged DJANGO_DATABASE_URL does not match the production contract."
        )
    return {
        "url": value,
        "role": expected_role,
        "database": expected_database,
        "password": unquote(parsed.password),
    }


def sql_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or not value[0].isalpha():
        raise MigrationError("Unsafe PostgreSQL identifier.")
    return f'"{value}"'


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql(sql: str, *, database: str = "postgres", capture: bool = False) -> str:
    return run(
        ["psql", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At", "-d", database],
        user="postgres",
        input_text=sql,
        capture=capture,
    )


def app_manage(args, *arguments: str, database_url: str | None = None, **kwargs) -> str:
    environment = {"DJANGO_DATABASE_URL": database_url} if database_url else None
    return run(
        [str(args.venv_python), str(args.project_dir / "manage.py"), *arguments],
        user=args.app_user,
        environment=environment,
        **kwargs,
    )


def prepare_postgresql(args, database: dict[str, str]) -> None:
    version = psql("SHOW server_version_num;", capture=True)
    if not version.startswith("17"):
        raise MigrationError("The production PostgreSQL server is not major version 17.")

    role = sql_identifier(database["role"])
    database_name = sql_identifier(database["database"])
    role_exists = psql(
        f"SELECT 1 FROM pg_roles WHERE rolname = {sql_literal(database['role'])};",  # noqa: S608
        capture=True,
    )
    role_sql = (
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"NOREPLICATION PASSWORD {sql_literal(database['password'])};"
        if role_exists == "1"
        else f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"NOREPLICATION PASSWORD {sql_literal(database['password'])};"
    )
    psql(role_sql)

    database_exists = psql(
        f"SELECT 1 FROM pg_database WHERE datname = {sql_literal(database['database'])};",  # noqa: S608
        capture=True,
    )
    if database_exists != "1":
        run(
            [
                "createdb",
                "--owner",
                database["role"],
                "--encoding",
                "UTF8",
                database["database"],
            ],
            user="postgres",
        )
    owner = psql(
        "SELECT pg_get_userbyid(datdba) FROM pg_database "  # noqa: S608
        f"WHERE datname = {sql_literal(database['database'])};",
        capture=True,
    )
    if owner != database["role"]:
        raise MigrationError("The PostgreSQL database exists with an unexpected owner.")

    psql(
        f"REVOKE ALL ON DATABASE {database_name} FROM PUBLIC;\n"
        f"GRANT CONNECT, TEMPORARY ON DATABASE {database_name} TO {role};",
    )
    psql(
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
        f"GRANT USAGE, CREATE ON SCHEMA public TO {role};",
        database=database["database"],
    )
    role_flags = psql(
        "SELECT (CASE WHEN rolcanlogin THEN '1' ELSE '0' END) || ':' || "  # noqa: S608
        "(CASE WHEN rolsuper THEN '1' ELSE '0' END) || ':' || "
        "(CASE WHEN rolcreatedb THEN '1' ELSE '0' END) || ':' || "
        "(CASE WHEN rolcreaterole THEN '1' ELSE '0' END) || ':' || "
        "(CASE WHEN rolreplication THEN '1' ELSE '0' END) FROM pg_roles "
        f"WHERE rolname = {sql_literal(database['role'])};",
        capture=True,
    )
    if role_flags != "1:0:0:0:0":
        raise MigrationError("The PostgreSQL application role has unexpected capabilities.")

    app_manage(args, "migrate", "--noinput", database_url=database["url"])
    app_manage(args, "check", "--deploy", database_url=database["url"])
    app_manage(args, "check_postgresql_concurrency", database_url=database["url"])
    empty_check = (
        "from django.apps import apps; "
        "allowed={'contenttypes.contenttype','auth.permission'}; "
        "unexpected={m._meta.label_lower:m._base_manager.count() for m in apps.get_models() "
        "if m._meta.managed and not m._meta.proxy and m._meta.label_lower not in allowed "
        "and m._base_manager.exists()}; "
        "assert not unexpected, unexpected"
    )
    app_manage(args, "shell", "-c", empty_check, database_url=database["url"])
    reset_sequences(args, database_url=database["url"])
    app_manage(args, "validate_postgresql_sequences", database_url=database["url"])
    write_check = (
        "from django.db import connection; "
        "assert connection.vendor == 'postgresql'; "
        "c=connection.cursor(); c.execute('CREATE TEMP TABLE oi_write_check (id integer)'); "
        "c.execute('INSERT INTO oi_write_check VALUES (1)'); "
        "c.execute('SELECT count(*) FROM oi_write_check'); "
        "assert c.fetchone()[0] == 1"
    )
    app_manage(args, "shell", "-c", write_check, database_url=database["url"])


def reset_sequences(args, *, database_url: str | None = None) -> None:
    labels_code = (
        "from django.apps import apps; from django.db import models; "
        "print(' '.join(sorted({m._meta.app_label for m in apps.get_models() "
        "if m._meta.managed and not m._meta.proxy and isinstance(m._meta.pk, models.AutoField)})))"
    )
    labels = app_manage(
        args,
        "shell",
        "--no-imports",
        "-c",
        labels_code,
        database_url=database_url,
        capture=True,
    ).split()
    sql = app_manage(
        args,
        "sqlsequencereset",
        "--database",
        "default",
        *labels,
        database_url=database_url,
        capture=True,
    )
    sql = "\n".join(
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;", "BEGIN", "COMMIT"}
    ).strip()
    if not sql:
        raise MigrationError("Django did not produce PostgreSQL sequence reset SQL.")
    app_manage(
        args,
        "dbshell",
        "--",
        "-v",
        "ON_ERROR_STOP=1",
        "-1",
        database_url=database_url,
        input_text=sql + "\n",
    )


def open_lock_file(path: Path, app_user: str):
    """Open a shared lock without O_CREAT on an existing sticky-directory file."""

    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o664)
        except FileExistsError:
            descriptor = os.open(path, os.O_RDWR)
        else:
            account = pwd.getpwnam(app_user)
            os.fchown(descriptor, account.pw_uid, account.pw_gid)
    return os.fdopen(descriptor, "r+")


def acquire_locks(project_name: str, app_user: str):
    handles = []
    for suffix in ("deploy", "ingest", "scheduler", "dns", "retention", "backup"):
        path = Path(f"/run/lock/{project_name}-{suffix}.lock")
        handle = open_lock_file(path, app_user)
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MigrationError(f"Timed out acquiring the {suffix} lock.") from None
                time.sleep(0.25)
        handles.append(handle)
    return handles


def service(args, action: str) -> None:
    run(
        [
            "systemctl",
            action,
            f"app@{args.project_name}.socket",
            f"app@{args.project_name}.service",
            args.mcp_service,
        ]
    )


def restore_sqlite_runtime(args, env_backup: Path | None) -> None:
    if env_backup and env_backup.is_file():
        shutil.copy2(env_backup, args.runtime_env)
        account = pwd.getpwnam(args.app_user)
        os.chown(args.runtime_env, account.pw_uid, account.pw_gid)
        args.runtime_env.chmod(0o600)
    run(
        [
            "systemctl",
            "reset-failed",
            f"app@{args.project_name}.service",
            f"app@{args.project_name}.socket",
            args.mcp_service,
        ]
    )
    run(["systemctl", "restart", f"app@{args.project_name}.socket", args.mcp_service])


def verify_sqlite(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        quick_check = database.execute("PRAGMA quick_check").fetchone()
        foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
    if not quick_check or quick_check[0] != "ok" or foreign_keys:
        raise MigrationError("SQLite integrity or foreign-key validation failed.")
    return {"quick_check": quick_check[0], "foreign_key_violations": len(foreign_keys)}


def cutover(args, database: dict[str, str]) -> None:
    lock_handles = []
    env_backup: Path | None = None
    services_opened = False
    try:
        lock_handles = acquire_locks(args.project_name, args.app_user)
        service(args, "stop")
        fuser = shutil.which("fuser")
        if fuser:
            result = subprocess.run(  # noqa: S603 - fuser is a fixed system diagnostic.
                [fuser, str(args.sqlite_path)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                raise MigrationError("A process still has the SQLite database open.")

        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        cutover_dir = args.backup_dir / "cutover" / timestamp
        cutover_dir.mkdir(parents=True, mode=0o700)
        cutover_dir.chmod(0o700)
        env_backup = cutover_dir / "runtime.env.sqlite"
        shutil.copy2(args.runtime_env, env_backup)
        env_backup.chmod(0o600)

        app_manage(args, "backup_database")
        sqlite_result = verify_sqlite(args.sqlite_path)
        sqlite_copy = cutover_dir / "db.sqlite3"
        shutil.copy2(args.sqlite_path, sqlite_copy)
        sqlite_copy.chmod(0o600)
        sqlite_result["sha256"] = run(["sha256sum", str(sqlite_copy)], capture=True).split()[0]
        (cutover_dir / "sqlite-validation.json").write_text(
            json.dumps(sqlite_result, sort_keys=True) + "\n"
        )

        source_manifest = cutover_dir / "source-manifest.json"
        source_manifest.write_text(app_manage(args, "database_manifest", capture=True) + "\n")
        source_manifest.chmod(0o600)
        fixture = cutover_dir / "sqlite-fixture.json"
        with fixture.open("w") as fixture_output:
            app_manage(
                args,
                "dumpdata",
                "--natural-foreign",
                "--exclude",
                "contenttypes",
                "--exclude",
                "auth.permission",
                stdout=fixture_output,
            )
        fixture.chmod(0o600)

        run(
            [
                str(args.venv_python),
                str(args.project_dir / ".deploy" / "sync_env.py"),
                str(args.runtime_env),
                str(args.staged_env),
            ],
            user=args.app_user,
        )
        fixture_for_app = args.project_dir / ".cutover-fixture.json"
        shutil.copy2(fixture, fixture_for_app)
        account = pwd.getpwnam(args.app_user)
        os.chown(fixture_for_app, account.pw_uid, account.pw_gid)
        fixture_for_app.chmod(0o600)
        try:
            app_manage(args, "loaddata", str(fixture_for_app))
        finally:
            fixture_for_app.unlink(missing_ok=True)
        reset_sequences(args)

        manifest_for_app = args.project_dir / ".cutover-source-manifest.json"
        shutil.copy2(source_manifest, manifest_for_app)
        os.chown(manifest_for_app, account.pw_uid, account.pw_gid)
        manifest_for_app.chmod(0o600)
        try:
            app_manage(args, "validate_database_manifest", str(manifest_for_app))
        finally:
            manifest_for_app.unlink(missing_ok=True)
        app_manage(args, "validate_postgresql_sequences")
        app_manage(args, "check", "--deploy")
        vendor_check = (
            "from django.db import connection; assert connection.vendor == 'postgresql'; "
            "c=connection.cursor(); c.execute('CREATE TEMP TABLE oi_cutover_check (id integer)'); "
            "c.execute('INSERT INTO oi_cutover_check VALUES (1)')"
        )
        app_manage(args, "shell", "-c", vendor_check)
        app_manage(args, "backup_database")
        run(["pgbackrest", "--stanza=stageops", "--type=diff", "backup"], user="postgres")

        run(
            [
                "systemctl",
                "reset-failed",
                f"app@{args.project_name}.service",
                f"app@{args.project_name}.socket",
                args.mcp_service,
            ]
        )
        run(["systemctl", "restart", f"app@{args.project_name}.socket", args.mcp_service])
        services_opened = True
        print(f"cutover_backup={cutover_dir}")
    except Exception as exc:
        if services_opened:
            service(args, "stop")
            print(f"PostgreSQL roll-forward required: {exc}", file=sys.stderr)
            raise SystemExit(30) from exc
        try:
            restore_sqlite_runtime(args, env_backup)
        except Exception as rollback_exc:
            print(f"SQLite rollback also failed: {rollback_exc}", file=sys.stderr)
        print(f"Cutover rolled back to SQLite: {exc}", file=sys.stderr)
        raise SystemExit(20) from exc
    finally:
        for handle in reversed(lock_handles):
            handle.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "cutover"))
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--staged-env", type=Path, required=True)
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--venv-python", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--app-user", required=True)
    parser.add_argument("--mcp-service", required=True)
    parser.add_argument("--database-role", required=True)
    parser.add_argument("--database-name", required=True)
    return parser


def main() -> None:
    os.umask(0o077)
    args = build_parser().parse_args()
    database = parse_database_url(args.staged_env, args.database_role, args.database_name)
    if args.action == "prepare":
        prepare_postgresql(args, database)
        print("PostgreSQL preparation checks passed.")
    else:
        cutover(args, database)


if __name__ == "__main__":
    main()
