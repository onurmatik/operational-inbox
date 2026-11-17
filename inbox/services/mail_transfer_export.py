"""Export retained mail without bypassing attachment quarantine or retention."""

from __future__ import annotations

import tempfile
import time
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from inbox.models import MailTransferItem, Message, OutboundMessage, RetentionPolicy
from inbox.services.mail_transfers import (
    BATCH_SIZE,
    finish,
    locked_transfer,
    missing_object,
    prefix,
)
from inbox.services.mbox import encode_entry
from inbox.services.mime import MAX_MIME_BYTES, parse_mime
from inbox.services.retention import _effective_retention_days


def _index(transfer):
    messages = Message.objects.filter(
        domain=transfer.domain, created_at__lte=transfer.created_at, domain_tests__isnull=True
    ).order_by("id")
    if transfer.checkpoint.get("last_id"):
        messages = messages.filter(id__gt=transfer.checkpoint["last_id"])
    rows = list(messages[:BATCH_SIZE])
    with locked_transfer(transfer) as current:
        position = int(current.checkpoint.get("position", 0))
        for message in rows:
            position += 1
            MailTransferItem.objects.get_or_create(
                transfer=current,
                position=position,
                defaults={"message": message, "message_at": message.received_at},
            )
        if rows:
            current.checkpoint = {"last_id": str(rows[-1].id), "position": position}
        else:
            current.phase = "export"
        current.save(update_fields=("checkpoint", "phase"))


def _object_bytes(client, key):
    try:
        response = client.get_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=key)
    except Exception as exc:
        if missing_object(exc):
            return None
        raise
    try:
        value = response["Body"].read(MAX_MIME_BYTES + 1)
    finally:
        response["Body"].close()
    if len(value) > MAX_MIME_BYTES:
        return None
    return value


def _render(transfer, message, client):
    now = timezone.now()
    retained_from = message.imported_at or message.received_at
    retention, _ = RetentionPolicy.objects.get_or_create(domain=transfer.domain)

    def expired(field):
        return (
            retained_from
            + timedelta(days=_effective_retention_days(transfer.domain, retention, field))
            <= now
        )

    if message.normalized_purged_at or expired("normalized_content_days"):
        return None, "content_expired", 0, False
    if message.is_quarantined or message.virus_verdict == Message.Verdict.FAIL:
        return None, "quarantined", 0, False
    attachments = list(message.attachments.all())
    clean = [
        a
        for a in attachments
        if a.scan_status == "CLEAN"
        and a.s3_key
        and not a.purged_at
        and a.purge_at > now
        and not expired("attachment_days")
    ]
    omitted = len(attachments) - len(clean)
    raw = None
    parsed = None
    if message.raw_s3_key and not message.raw_purged_at and not expired("raw_message_days"):
        raw = _object_bytes(client, message.raw_s3_key)
        if raw is not None:
            try:
                parsed = parse_mime(raw)
            except ValidationError:
                raw = None
    # Untracked MIME parts must never be smuggled past attachment authorization.
    if (
        raw is not None
        and parsed is not None
        and message.virus_verdict == "PASS"
        and not omitted
        and sorted(a.sha256 for a in parsed.attachments) == sorted(a.sha256 for a in clean)
    ):
        return raw, "", 0, False

    mime = EmailMessage(policy=policy.SMTP)
    mime["From"] = message.from_address
    if message.reply_to_address:
        mime["Reply-To"] = message.reply_to_address
    for kind, header in (("TO", "To"), ("CC", "Cc"), ("BCC", "Bcc")):
        addresses = list(message.recipients.filter(kind=kind).values_list("address", flat=True))
        if addresses:
            mime[header] = ", ".join(addresses)
    mime["Subject"] = message.subject
    if message.sent_at:
        mime["Date"] = format_datetime(message.sent_at)
    if message.rfc_message_id:
        mime["Message-ID"] = message.rfc_message_id
    if parsed:
        if parsed.in_reply_to:
            mime["In-Reply-To"] = " ".join(parsed.in_reply_to)
        if parsed.references:
            mime["References"] = " ".join(parsed.references)
    elif message.direction == "OUTBOUND":
        outbound = (
            OutboundMessage.objects.filter(
                domain=transfer.domain, provider_message_id=message.provider_message_id
            )
            .select_related("revision__draft__context_message")
            .first()
        )
        if outbound and outbound.revision.draft.context_message.rfc_message_id:
            mime["In-Reply-To"] = outbound.revision.draft.context_message.rfc_message_id
            mime["References"] = outbound.revision.draft.context_message.rfc_message_id
    mime.set_content(message.text_body)
    if message.html_body:
        mime.add_alternative(message.html_body, subtype="html")
    for attachment in clean:
        data = _object_bytes(client, attachment.s3_key)
        if data is None:
            omitted += 1
            continue
        maintype, separator, subtype = attachment.content_type.partition("/")
        if (
            not separator
            or not maintype.isalnum()
            or not subtype.replace("-", "").replace(".", "").replace("+", "").isalnum()
        ):
            maintype, subtype = "application", "octet-stream"
        mime.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=attachment.display_name
        )
    return mime.as_bytes(), "", omitted, True


def _export(transfer, item, client):
    if item.message is None:
        raw, error, omitted, reconstructed = None, "content_expired", 0, False
    else:
        raw, error, omitted, reconstructed = _render(transfer, item.message, client)
    key = ""
    size = 0
    if raw is not None:
        data = encode_entry(raw, sender=item.message.from_address, at=item.message.received_at)
        size = len(data)
        key = prefix(transfer) + f"chunks/{item.position}/{transfer.worker_token}.mbox"
        client.put_object(
            Bucket=settings.AWS_INGRESS_BUCKET,
            Key=key,
            Body=data,
            ContentType="application/mbox",
            ServerSideEncryption="AES256",
        )
    with locked_transfer(transfer):
        item.status = "EXPORTED" if raw is not None else "SKIPPED"
        item.error_code = error
        item.object_key = key
        item.output_size = size
        item.omitted_attachments = omitted
        item.reconstructed = reconstructed
        item.save(
            update_fields=(
                "status",
                "error_code",
                "object_key",
                "output_size",
                "omitted_attachments",
                "reconstructed",
            )
        )


def _assemble(transfer, client):
    position = int(transfer.checkpoint.get("assembled", 0))
    items = transfer.items.filter(status="EXPORTED", position__gt=position)
    if not items.exists():
        finish(transfer)
        return
    part = len(transfer.files) + 1
    key = prefix(transfer) + f"exports/{part:04d}-{transfer.worker_token}.mbox"
    size = 0
    deadline = time.monotonic() + 20
    with tempfile.TemporaryFile() as output:
        for item in items.iterator(chunk_size=100):
            if size and size + item.output_size > settings.MAIL_TRANSFER_MAX_BYTES:
                break
            if item.output_size > settings.MAIL_TRANSFER_MAX_BYTES:
                raise ValidationError(
                    "Message exceeds export size limit.", code="message_too_large"
                )
            body = client.get_object(Bucket=settings.AWS_INGRESS_BUCKET, Key=item.object_key)[
                "Body"
            ]
            try:
                while chunk := body.read(64 * 1024):
                    output.write(chunk)
            finally:
                body.close()
            size += item.output_size
            position = item.position
            if time.monotonic() >= deadline:
                break
        output.seek(0)
        client.upload_fileobj(
            output,
            settings.AWS_INGRESS_BUCKET,
            key,
            ExtraArgs={"ContentType": "application/mbox", "ServerSideEncryption": "AES256"},
        )
    with locked_transfer(transfer) as current:
        current.files = [*current.files, {"key": key, "size": size}]
        current.checkpoint = {"assembled": position}
        current.save(update_fields=("files", "checkpoint"))


def export_batch(transfer, client):
    if transfer.phase == "index":
        _index(transfer)
    elif transfer.phase == "export":
        items = list(transfer.items.filter(status="PENDING").select_related("message")[:BATCH_SIZE])
        if not items:
            with locked_transfer(transfer) as current:
                current.phase = "assemble"
                current.checkpoint = {"assembled": 0}
                current.save(update_fields=("phase", "checkpoint"))
            return
        deadline = time.monotonic() + 20
        for item in items:
            _export(transfer, item, client)
            if time.monotonic() >= deadline:
                break
    else:
        _assemble(transfer, client)
