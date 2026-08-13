from __future__ import annotations

import base64
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

MAGIC = b"OIBACKUP1"


def encryption_key() -> bytes:
    value = settings.BACKUP_ENCRYPTION_KEY
    try:
        key = base64.urlsafe_b64decode(value)
    except Exception as exc:
        raise CommandError("BACKUP_ENCRYPTION_KEY must be a URL-safe base64 value.") from exc
    if len(key) != 32:
        raise CommandError("BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def _encrypt(source: Path, destination: Path) -> None:
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(encryption_key()), modes.GCM(nonce)).encryptor()
    with source.open("rb") as plain, destination.open("wb") as encrypted:
        encrypted.write(MAGIC)
        encrypted.write(nonce)
        while chunk := plain.read(1024 * 1024):
            encrypted.write(encryptor.update(chunk))
        encrypted.write(encryptor.finalize())
        encrypted.write(encryptor.tag)
    destination.chmod(0o600)


def _upload_and_verify(path: Path, *, object_prefix: str) -> str:
    if not settings.AWS_INGRESS_BUCKET:
        return "disabled"
    checksum = hashlib.sha256()
    with path.open("rb") as encrypted:
        while chunk := encrypted.read(1024 * 1024):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    object_key = f"{object_prefix}/{path.name}"
    s3 = boto3.client("s3", region_name=settings.AWS_REGION)
    s3.upload_file(
        str(path),
        settings.AWS_INGRESS_BUCKET,
        object_key,
        ExtraArgs={
            "ServerSideEncryption": "AES256",
            "Metadata": {"sha256": digest},
        },
    )
    remote = s3.head_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=object_key)
    if (
        int(remote.get("ContentLength", -1)) != path.stat().st_size
        or remote.get("Metadata", {}).get("sha256") != digest
    ):
        raise CommandError("The encrypted S3 backup failed checksum verification.")
    return object_key


def _expire_local_backups(destination_dir: Path, *, pattern: str, current: Path) -> int:
    cutoff = timezone.now() - timedelta(days=30)
    removed = 0
    for candidate in destination_dir.glob(pattern):
        if candidate == current:
            continue
        modified = datetime.fromtimestamp(
            candidate.stat().st_mtime,
            tz=timezone.get_current_timezone(),
        )
        if modified < cutoff:
            candidate.unlink()
            removed += 1
    return removed


def _sqlite_snapshot(database: dict[str, Any], destination: Path) -> None:
    source_path = Path(str(database["NAME"])).resolve()
    if not source_path.is_file():
        raise CommandError(f"SQLite database does not exist: {source_path}")
    with sqlite3.connect(source_path) as source:
        source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise CommandError("Source SQLite integrity_check did not return ok.")
        with sqlite3.connect(destination) as target:
            source.backup(target)
            backup_integrity = target.execute("PRAGMA integrity_check").fetchone()
            if not backup_integrity or backup_integrity[0] != "ok":
                raise CommandError("Backup SQLite integrity_check did not return ok.")


def _postgresql_snapshot(database: dict[str, Any], destination: Path) -> None:
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise CommandError("pg_dump and pg_restore are required for PostgreSQL backups.")

    command = [pg_dump, "--format=custom", "--no-password", f"--file={destination}"]
    for option, value in (
        ("--host", database.get("HOST")),
        ("--port", database.get("PORT")),
        ("--username", database.get("USER")),
        ("--dbname", database.get("NAME")),
    ):
        if value:
            command.extend((option, str(value)))
    environment = os.environ.copy()
    password = str(database.get("PASSWORD") or "")
    if password:
        environment["PGPASSWORD"] = password
    result = subprocess.run(  # noqa: S603 - executables are resolved to absolute system paths.
        command,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError("pg_dump failed; its output was suppressed to protect credentials.")
    result = subprocess.run(  # noqa: S603 - executable is an absolute system path.
        [pg_restore, "--list", str(destination)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CommandError("pg_restore could not inspect the PostgreSQL backup.")


class Command(BaseCommand):
    help = "Create an encrypted, verified backup of the configured database."

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        engine = database["ENGINE"]
        destination_dir = Path(settings.BACKUP_DIRECTORY).resolve()
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")

        if engine == "django.db.backends.sqlite3":
            suffix = "sqlite3"
            object_prefix = "backups/sqlite"
            snapshot = _sqlite_snapshot
        elif engine == "django.db.backends.postgresql":
            suffix = "pgdump"
            object_prefix = "backups/postgresql"
            snapshot = _postgresql_snapshot
        else:
            raise CommandError("backup_database supports only SQLite and PostgreSQL.")

        output_path = destination_dir / f"operationalinbox-{timestamp}.{suffix}.aesgcm"
        with tempfile.NamedTemporaryFile(suffix=f".{suffix}") as temporary:
            snapshot(database, Path(temporary.name))
            _encrypt(Path(temporary.name), output_path)

        offsite = _upload_and_verify(output_path, object_prefix=object_prefix)
        removed = _expire_local_backups(
            destination_dir,
            pattern=f"operationalinbox-*.{suffix}.aesgcm",
            current=output_path,
        )
        self.stdout.write(f"backup={output_path} offsite={offsite} expired_removed={removed}")
