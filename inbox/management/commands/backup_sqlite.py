from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

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


class Command(BaseCommand):
    help = "Create an online, integrity-checked, AES-GCM encrypted SQLite backup."

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if database["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("backup_sqlite only supports the SQLite deployment.")
        source_path = Path(str(database["NAME"])).resolve()
        if not source_path.is_file():
            raise CommandError(f"SQLite database does not exist: {source_path}")
        destination_dir = Path(settings.BACKUP_DIRECTORY).resolve()
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        timestamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        output_path = destination_dir / f"operationalinbox-{timestamp}.sqlite3.aesgcm"
        key = encryption_key()
        with sqlite3.connect(source_path) as source:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise CommandError("Source SQLite integrity_check did not return ok.")
            with tempfile.NamedTemporaryFile(suffix=".sqlite3") as temporary:
                with sqlite3.connect(temporary.name) as target:
                    source.backup(target)
                    backup_integrity = target.execute("PRAGMA integrity_check").fetchone()
                    if not backup_integrity or backup_integrity[0] != "ok":
                        raise CommandError("Backup SQLite integrity_check did not return ok.")
                nonce = os.urandom(12)
                encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
                temporary.seek(0)
                with output_path.open("wb") as encrypted:
                    encrypted.write(MAGIC)
                    encrypted.write(nonce)
                    while chunk := temporary.read(1024 * 1024):
                        encrypted.write(encryptor.update(chunk))
                    encrypted.write(encryptor.finalize())
                    encrypted.write(encryptor.tag)
        output_path.chmod(0o600)
        offsite = "disabled"
        if settings.AWS_INGRESS_BUCKET:
            checksum = hashlib.sha256()
            with output_path.open("rb") as encrypted_backup:
                while chunk := encrypted_backup.read(1024 * 1024):
                    checksum.update(chunk)
            digest = checksum.hexdigest()
            object_key = f"backups/sqlite/{output_path.name}"
            s3 = boto3.client("s3", region_name=settings.AWS_REGION)
            s3.upload_file(
                str(output_path),
                settings.AWS_INGRESS_BUCKET,
                object_key,
                ExtraArgs={
                    "ServerSideEncryption": "AES256",
                    "Metadata": {"sha256": digest},
                },
            )
            remote = s3.head_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=object_key)
            if (
                int(remote.get("ContentLength", -1)) != output_path.stat().st_size
                or remote.get("Metadata", {}).get("sha256") != digest
            ):
                raise CommandError("The encrypted S3 backup failed checksum verification.")
            offsite = object_key
        cutoff = timezone.now() - timedelta(days=30)
        removed = 0
        for candidate in destination_dir.glob("operationalinbox-*.sqlite3.aesgcm"):
            if candidate == output_path:
                continue
            modified = datetime.fromtimestamp(
                candidate.stat().st_mtime, tz=timezone.get_current_timezone()
            )
            if modified < cutoff:
                candidate.unlink()
                removed += 1
        self.stdout.write(f"backup={output_path} offsite={offsite} expired_removed={removed}")
