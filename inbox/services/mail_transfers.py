"""Owner-scoped mail transfers using private S3 objects and resumable durable jobs."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import PurePath

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from inbox.models import AuditEvent, Domain, DurableJob, MailTransfer
from inbox.services.entitlements import can_manage_domain

ACTIVE = (MailTransfer.Status.UPLOADING, MailTransfer.Status.QUEUED, MailTransfer.Status.RUNNING)
TERMINAL = (MailTransfer.Status.COMPLETE, MailTransfer.Status.FAILED, MailTransfer.Status.EXPIRED)
BATCH_SIZE = 25
logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


def s3_client():
    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
    )


def prefix(transfer):
    return f"domains/{transfer.domain_id}/transfers/{transfer.id}/"


def can_import(domain, owner):
    return (
        owner.is_active
        and owner.is_email_verified
        and domain.owner_id == owner.id
        and domain.status != Domain.Status.DISABLED
        and domain.ownership_verified
        and can_manage_domain(owner, domain)
    )


def audit(transfer, event):
    AuditEvent.objects.create(
        domain=transfer.domain,
        actor_type=AuditEvent.ActorType.OWNER,
        actor_id=transfer.requested_by_id,
        event_type=f"mail_transfer.{event}",
        object_type="MailTransfer",
        object_id=transfer.id,
        request_id=f"transfer:{transfer.id}",
        metadata={"kind": transfer.kind, "error_code": transfer.error_code},
    )


def _queue(transfer):
    from inbox.services.jobs import enqueue_job

    enqueue_job(
        kind="mail_transfer",
        idempotency_key=f"mail-transfer:{transfer.id}:{transfer.generation}",
        domain=transfer.domain,
        payload={"transfer_id": str(transfer.id), "generation": transfer.generation},
    )


def start_transfer(*, domain, owner, kind, filename="", size=0):
    if domain.owner_id != owner.id or not owner.is_active or not owner.is_email_verified:
        raise ValidationError("This domain is unavailable.", code="domain_unavailable")
    if kind == MailTransfer.Kind.IMPORT:
        if not can_import(domain, owner):
            raise ValidationError("Import requires a verified, writable domain.", code="read_only")
        if not 0 < size <= settings.MAIL_TRANSFER_MAX_BYTES:
            raise ValidationError("Choose an MBOX file of up to 250 MB.", code="file_size")
    elif kind != MailTransfer.Kind.EXPORT:
        raise ValidationError("Invalid transfer kind.")
    try:
        with transaction.atomic():
            # Coordinate competing requests on PostgreSQL as well as the partial unique index.
            locked_domain = Domain.objects.select_for_update().get(id=domain.id, owner=owner)
            if kind == MailTransfer.Kind.IMPORT and not can_import(locked_domain, owner):
                raise ValidationError(
                    "Import requires a verified, writable domain.", code="read_only"
                )
            if MailTransfer.objects.filter(domain=domain, status__in=ACTIVE).exists():
                raise ValidationError("A transfer is already in progress.", code="transfer_active")
            transfer = MailTransfer.objects.create(
                domain=domain,
                requested_by=owner,
                kind=kind,
                status=MailTransfer.Status.UPLOADING
                if kind == "IMPORT"
                else MailTransfer.Status.QUEUED,
                filename=PurePath(filename.replace("\\", "/")).name[:255],
                source_size=size,
                expires_at=timezone.now() + timedelta(hours=24),
            )
            if kind == "IMPORT":
                transfer.upload_key = prefix(transfer) + "upload.mbox"
                transfer.save(update_fields=("upload_key",))
            else:
                transaction.on_commit(lambda: _queue(transfer))
            audit(transfer, "started")
            return transfer
    except IntegrityError as exc:
        raise ValidationError("A transfer is already in progress.", code="transfer_active") from exc


def upload_form(transfer, *, client=None):
    client = client or s3_client()
    return client.generate_presigned_post(
        Bucket=settings.AWS_INGRESS_BUCKET,
        Key=transfer.upload_key,
        Fields={"Content-Type": "application/octet-stream"},
        Conditions=[
            ["content-length-range", 1, settings.MAIL_TRANSFER_MAX_BYTES],
            {"Content-Type": "application/octet-stream"},
        ],
        ExpiresIn=900,
    )


def complete_upload(transfer, *, client=None):
    client = client or s3_client()
    with transaction.atomic():
        current = (
            MailTransfer.objects.select_for_update()
            .select_related("domain__owner")
            .get(id=transfer.id)
        )
        if current.kind != MailTransfer.Kind.IMPORT:
            raise ValidationError("Invalid upload.")
        if current.status != MailTransfer.Status.UPLOADING:
            return current  # A repeated browser callback must never replace the sealed source.
        if current.expires_at <= timezone.now() or not can_import(
            current.domain, current.requested_by
        ):
            raise ValidationError("This upload is no longer available.", code="upload_expired")
        head = client.head_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=current.upload_key)
        if (
            head["ContentLength"] != current.source_size
            or not 0 < head["ContentLength"] <= settings.MAIL_TRANSFER_MAX_BYTES
        ):
            raise ValidationError("The uploaded file size does not match.", code="file_size")
        current.source_key = prefix(current) + "source.mbox"
        # A reused upload form cannot mutate the worker's source. ETag fences replacement
        # between HEAD and COPY; multipart ETags are compared, never treated as checksums.
        client.copy_object(
            Bucket=settings.AWS_INGRESS_BUCKET,
            Key=current.source_key,
            CopySource={"Bucket": settings.AWS_INGRESS_BUCKET, "Key": current.upload_key},
            CopySourceIfMatch=head["ETag"],
            ServerSideEncryption="AES256",
        )
        current.status = MailTransfer.Status.QUEUED
        current.save(update_fields=("source_key", "status", "updated_at"))
        transaction.on_commit(lambda: _queue(current))
        return current


@contextmanager
def locked_transfer(transfer):
    with transaction.atomic():
        current = MailTransfer.objects.select_for_update().get(id=transfer.id)
        if (
            current.worker_token != transfer.worker_token
            or current.status != MailTransfer.Status.RUNNING
        ):
            raise LeaseLost
        if current.kind == MailTransfer.Kind.IMPORT:
            domain = (
                Domain.objects.select_for_update().select_related("owner").get(id=current.domain_id)
            )
            if not can_import(domain, current.requested_by):
                raise ValidationError("This domain is now read-only.", code="read_only")
        yield current


def missing_object(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in {
        "NoSuchKey",
        "404",
        "NotFound",
    }


def finish(transfer):
    with locked_transfer(transfer) as current:
        current.status = MailTransfer.Status.COMPLETE
        current.completed_at = timezone.now()
        current.expires_at = timezone.now() + timedelta(hours=24)
        current.save(update_fields=("status", "completed_at", "expires_at", "updated_at"))
        current.items.update(headers={})
        audit(current, "completed")


def fail_transfer(transfer_id, code, *, token=None, expected_status=None, generation=None):
    with transaction.atomic():
        current = (
            MailTransfer.objects.select_for_update()
            .filter(id=transfer_id, status__in=ACTIVE)
            .first()
        )
        if current is None or (token is not None and current.worker_token != token):
            return
        if expected_status is not None and current.status != expected_status:
            return
        if generation is not None and current.generation != generation:
            return
        current.status = MailTransfer.Status.FAILED
        current.error_code = code
        current.completed_at = timezone.now()
        current.leased_until = None
        current.worker_token = None
        current.save()
        current.items.update(headers={})
        audit(current, "failed")


def process_transfer(transfer_id, *, client=None, generation=None):
    from inbox.services.mail_transfer_export import export_batch
    from inbox.services.mail_transfer_import import import_batch

    client = client or s3_client()
    now = timezone.now()
    with transaction.atomic():
        transfer = (
            MailTransfer.objects.select_for_update()
            .select_related("domain__owner", "requested_by")
            .get(id=transfer_id)
        )
        if transfer.status not in (MailTransfer.Status.QUEUED, MailTransfer.Status.RUNNING):
            return
        if generation is not None and transfer.generation != generation:
            return
        if transfer.leased_until and transfer.leased_until > now:
            return
        if (
            transfer.domain.owner_id != transfer.requested_by_id
            or not transfer.requested_by.is_active
        ):
            fail_transfer(transfer.id, "domain_unavailable")
            return
        if transfer.kind == "IMPORT" and not can_import(transfer.domain, transfer.requested_by):
            fail_transfer(transfer.id, "read_only")
            return
        transfer.status = MailTransfer.Status.RUNNING
        transfer.worker_token = uuid.uuid4()
        transfer.leased_until = now + timedelta(minutes=4)
        transfer.save(update_fields=("status", "worker_token", "leased_until", "updated_at"))
    success = False
    try:
        if transfer.kind == "IMPORT":
            import_batch(transfer, client)
        else:
            export_batch(transfer, client)
        success = True
    except LeaseLost:
        return
    except ValidationError as exc:
        fail_transfer(
            transfer.id, getattr(exc, "code", None) or "invalid_mbox", token=transfer.worker_token
        )
    finally:
        with transaction.atomic():
            current = MailTransfer.objects.select_for_update().get(id=transfer.id)
            if current.worker_token == transfer.worker_token:
                current.worker_token = None
                current.leased_until = None
                if success and current.status == MailTransfer.Status.RUNNING:
                    current.generation += 1
                    transaction.on_commit(lambda: _queue(current))
                current.save(
                    update_fields=("worker_token", "leased_until", "generation", "updated_at")
                )


def transfer_summary(transfer):
    counts = {
        row["status"].lower(): row["count"]
        for row in transfer.items.values("status").annotate(count=Count("id"))
    }
    extras = transfer.items.aggregate(
        omitted=Sum("omitted_attachments"), reconstructed=Count("id", filter=Q(reconstructed=True))
    )
    return {
        "id": str(transfer.id),
        "kind": transfer.kind,
        "status": transfer.status,
        "label": transfer.get_status_display(),
        "counts": counts,
        "omitted_attachments": extras["omitted"] or 0,
        "reconstructed": extras["reconstructed"],
        "error_code": transfer.error_code,
        "active": transfer.status in ACTIVE,
    }


def download_url(transfer, part, *, client=None):
    if (
        transfer.kind != "EXPORT"
        or transfer.status != "COMPLETE"
        or transfer.expires_at <= timezone.now()
    ):
        raise ValidationError("This export has expired or is not ready.", code="export_unavailable")
    if part < 0 or part >= len(transfer.files):
        raise ValidationError("This file is unavailable.", code="export_unavailable")
    file = transfer.files[part]
    client = client or s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.AWS_INGRESS_BUCKET,
            "Key": file["key"],
            "ResponseContentType": "application/mbox",
            "ResponseContentDisposition": (
                f'attachment; filename="{transfer.domain.hostname}-{part + 1:03d}.mbox"'
            ),
        },
        ExpiresIn=min(300, max(1, int((transfer.expires_at - timezone.now()).total_seconds()))),
    )


def maintain_transfers(*, client=None):
    """Repair enqueue gaps, expire abandoned uploads, and remove private scratch data."""
    now = timezone.now()
    count = 0
    for transfer in MailTransfer.objects.filter(status__in=ACTIVE):
        if transfer.expires_at <= now:
            fail_transfer(transfer.id, "transfer_expired")
        elif transfer.status != "UPLOADING" and (
            not transfer.leased_until or transfer.leased_until <= now
        ):
            key = f"mail-transfer:{transfer.id}:{transfer.generation}"
            job = DurableJob.objects.filter(idempotency_key=key).first()
            if job and job.status == DurableJob.Status.FAILED:
                fail_transfer(transfer.id, "processing_failed", generation=transfer.generation)
            elif job is None or job.status == DurableJob.Status.COMPLETE:
                with transaction.atomic():
                    current = MailTransfer.objects.select_for_update().get(id=transfer.id)
                    if (
                        current.status in ("QUEUED", "RUNNING")
                        and current.generation == transfer.generation
                        and (not current.leased_until or current.leased_until <= now)
                    ):
                        current.generation += 1
                        current.save(update_fields=("generation",))
                        _queue(current)
                        count += 1
    cleanup = (
        MailTransfer.objects.filter(status__in=TERMINAL)
        .filter(
            Q(cleaned_at__isnull=True) | Q(kind="EXPORT", status="COMPLETE", expires_at__lte=now)
        )
        .order_by("created_at")[:25]
    )
    for transfer in cleanup:
        client = client or s3_client()
        keep_exports = (
            transfer.kind == "EXPORT"
            and transfer.status == "COMPLETE"
            and transfer.expires_at > now
        )
        keep = {file["key"] for file in transfer.files} if keep_exports else set()
        try:
            for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=settings.AWS_INGRESS_BUCKET, Prefix=prefix(transfer)
            ):
                for obj in page.get("Contents", []):
                    if obj["Key"] not in keep:
                        client.delete_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=obj["Key"])
        except (BotoCoreError, ClientError):
            # Cleanup is retried next tick; a storage outage must not stop email jobs.
            logger.warning("Mail transfer cleanup deferred", extra={"transfer_id": transfer.id})
            continue
        if keep_exports:
            MailTransfer.objects.filter(id=transfer.id).update(cleaned_at=now)
        # An upload URL can still be reused for fifteen minutes after issue.
        if not keep_exports and transfer.created_at < now - timedelta(minutes=16):
            MailTransfer.objects.filter(id=transfer.id).update(
                cleaned_at=now, source_key="", upload_key="", files=[]
            )
        if (
            transfer.kind == "EXPORT"
            and transfer.status == "COMPLETE"
            and transfer.expires_at <= now
        ):
            MailTransfer.objects.filter(id=transfer.id).update(status=MailTransfer.Status.EXPIRED)
    MailTransfer.objects.filter(
        status__in=TERMINAL, cleaned_at__isnull=False, created_at__lt=now - timedelta(days=30)
    ).delete()
    return count
