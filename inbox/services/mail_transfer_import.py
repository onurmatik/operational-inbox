"""Historical MBOX ingestion; deliberately independent of live SES side effects."""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Max, Min, Q
from django.utils import timezone

from inbox.models import (
    Attachment,
    Conversation,
    DraftApproval,
    MailTransferItem,
    Message,
    MessageRecipient,
    MessageReference,
    OutboundMessage,
    ReplyDraft,
    RetentionPolicy,
)
from inbox.services.mail_transfers import BATCH_SIZE, finish, locked_transfer
from inbox.services.mbox import StreamingReader, decode_entry, entries, envelope_date
from inbox.services.mime import MAX_MIME_BYTES, parse_mime
from inbox.services.retention import _effective_retention_days
from inbox.services.threading import match_conversation, normalize_subject, reference_hash


def _headers(parsed):
    return {
        "id": parsed.rfc_message_id,
        "from": parsed.from_address,
        "to": list(parsed.to_addresses),
        "cc": list(parsed.cc_addresses),
        "bcc": list(parsed.bcc_addresses),
        "references": list(parsed.references),
        "in_reply_to": list(parsed.in_reply_to),
    }


def _index(transfer, client):
    offset = int(transfer.checkpoint.get("offset", 0))
    position = int(transfer.checkpoint.get("position", 0))
    if offset >= transfer.source_size:
        with locked_transfer(transfer) as current:
            current.phase = "import"
            current.save(update_fields=("phase",))
        return
    response = client.get_object(
        Bucket=settings.AWS_INGRESS_BUCKET, Key=transfer.source_key, Range=f"bytes={offset}-"
    )
    deadline = time.monotonic() + 20
    with io.BufferedReader(StreamingReader(response["Body"])) as stream:
        for entry in entries(stream, offset=offset):
            position += 1
            defaults = {"offset": entry.offset, "length": entry.length}
            try:
                if entry.oversized:
                    raise ValidationError("Message exceeds 40 MB.", code="message_too_large")
                parsed = parse_mime(decode_entry(entry.raw))
                defaults.update(
                    headers=_headers(parsed),
                    message_at=parsed.sent_at
                    or envelope_date(entry.envelope)
                    or transfer.created_at,
                )
            except ValidationError as exc:
                defaults.update(
                    status=MailTransferItem.Status.FAILED,
                    error_code=getattr(exc, "code", None) or "invalid_mime",
                )
            with locked_transfer(transfer) as current:
                MailTransferItem.objects.get_or_create(
                    transfer=current, position=position, defaults=defaults
                )
                current.checkpoint = {"offset": entry.offset + entry.length, "position": position}
                current.save(update_fields=("checkpoint",))
            if position % BATCH_SIZE == 0 or time.monotonic() >= deadline:
                return
    with locked_transfer(transfer) as current:
        current.phase = "import"
        current.save(update_fields=("phase",))


def _fingerprint(parsed):
    value = [
        parsed.rfc_message_id.casefold(),
        parsed.from_address,
        parsed.reply_to_address,
        sorted(parsed.to_addresses),
        sorted(parsed.cc_addresses),
        sorted(parsed.bcc_addresses),
        parsed.subject,
        parsed.sent_at.isoformat() if parsed.sent_at else "",
        parsed.text_body.strip(),
        parsed.html_body.strip(),
        sorted(a.sha256 for a in parsed.attachments),
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


def _same_content(message, parsed):
    def normalized(value):
        return value.replace("\r\n", "\n").strip()

    return (
        not message.normalized_purged_at
        and message.from_address.casefold() == parsed.from_address.casefold()
        and message.reply_to_address.casefold() == parsed.reply_to_address.casefold()
        and message.subject == parsed.subject
        and normalized(message.text_body) == normalized(parsed.text_body)
        and normalized(message.html_body) == normalized(parsed.html_body)
        and sorted(message.attachments.values_list("sha256", flat=True))
        == sorted(a.sha256 for a in parsed.attachments)
        and set(message.recipients.filter(kind="TO").values_list("address", flat=True))
        == set(parsed.to_addresses)
        and set(message.recipients.filter(kind="CC").values_list("address", flat=True))
        == set(parsed.cc_addresses)
        and set(message.recipients.filter(kind="BCC").values_list("address", flat=True))
        == set(parsed.bcc_addresses)
    )


def _duplicate(domain, parsed, fingerprint):
    existing = Message.objects.filter(
        domain=domain, provider_message_id=f"mbox:{fingerprint}"
    ).first()
    if existing:
        return existing, ""
    if parsed.rfc_message_id:
        candidates = list(
            Message.objects.filter(domain=domain, rfc_message_id__iexact=parsed.rfc_message_id)
        )
        for message in candidates:
            if _same_content(message, parsed):
                return message, ""
        if candidates:
            return candidates[0], "message_id_conflict"
    existing = Message.objects.filter(domain=domain, raw_sha256=parsed.raw_sha256).first()
    return existing, ""


def _invalidate(conversation):
    ReplyDraft.objects.filter(conversation=conversation, is_stale=False).update(is_stale=True)
    DraftApproval.objects.filter(
        revision__draft__conversation=conversation, invalidated_at__isnull=True
    ).update(
        invalidated_at=timezone.now(),
        invalidated_reason="imported_history",
    )


def _times(conversation):
    times = conversation.messages.aggregate(
        first=Min("received_at"),
        last=Max("received_at"),
        inbound=Max("received_at", filter=Q(direction="INBOUND")),
        outbound=Max("received_at", filter=Q(direction="OUTBOUND")),
    )
    if times["first"] is not None:
        conversation.first_message_at = times["first"]
        conversation.last_message_at = times["last"]
        conversation.last_inbound_at = times["inbound"]
        conversation.last_outbound_at = times["outbound"]
        conversation.save(
            update_fields=(
                "first_message_at",
                "last_message_at",
                "last_inbound_at",
                "last_outbound_at",
                "updated_at",
            )
        )


def _read_item(transfer, item, client):
    body = client.get_object(
        Bucket=settings.AWS_INGRESS_BUCKET,
        Key=transfer.source_key,
        Range=f"bytes={item.offset}-{item.offset + item.length - 1}",
    )["Body"]
    try:
        framed = body.read(MAX_MIME_BYTES + 4097)
    finally:
        body.close()
    return decode_entry(framed.partition(b"\n")[2])


def _store(transfer, item, client):
    raw = _read_item(transfer, item, client)
    parsed = parse_mime(raw)
    fingerprint = _fingerprint(parsed)
    existing, conflict = _duplicate(transfer.domain, parsed, fingerprint)
    if existing:
        with locked_transfer(transfer):
            item.status = "FAILED" if conflict else "DUPLICATE"
            item.error_code = conflict
            item.message = existing
            item.save(update_fields=("status", "error_code", "message"))
        return
    message_id = uuid.uuid5(transfer.domain_id, f"mbox:{fingerprint}")
    key = f"domains/{transfer.domain_id}/messages/{message_id}"
    raw_key = f"{key}/raw/message.eml"
    client.put_object(
        Bucket=settings.AWS_INGRESS_BUCKET,
        Key=raw_key,
        Body=raw,
        ContentType="message/rfc822",
        ServerSideEncryption="AES256",
    )
    attachment_keys = []
    for number, attachment in enumerate(parsed.attachments):
        attachment_key = f"{key}/attachments/{number}-{attachment.sha256}"
        client.put_object(
            Bucket=settings.AWS_INGRESS_BUCKET,
            Key=attachment_key,
            Body=attachment.content,
            ContentType="application/octet-stream",
            ServerSideEncryption="AES256",
        )
        attachment_keys.append(attachment_key)
    with locked_transfer(transfer):
        existing, conflict = _duplicate(transfer.domain, parsed, fingerprint)
        if existing:
            item.message = existing
            item.status = "FAILED" if conflict else "DUPLICATE"
            item.error_code = conflict
            item.save(update_fields=("message", "status", "error_code"))
            return
        recipients = (*parsed.to_addresses, *parsed.cc_addresses, *parsed.bcc_addresses)
        conversation = match_conversation(
            domain=transfer.domain, parsed=parsed, envelope_recipients=list(recipients)
        )
        at = item.message_at or transfer.created_at
        direction = (
            "OUTBOUND"
            if parsed.from_address.rpartition("@")[2].casefold() == transfer.domain.hostname
            else "INBOUND"
        )
        if conversation is None:
            conversation = Conversation.objects.create(
                domain=transfer.domain,
                subject=parsed.subject,
                normalized_subject=normalize_subject(parsed.subject),
                first_message_at=at,
                last_message_at=at,
            )
        else:
            conversation = Conversation.objects.select_for_update().get(id=conversation.id)
            _invalidate(conversation)
        imported_at = timezone.now()
        message = Message.objects.create(
            id=message_id,
            domain=transfer.domain,
            conversation=conversation,
            provider_message_id=f"mbox:{fingerprint}",
            rfc_message_id=parsed.rfc_message_id,
            direction=direction,
            from_address=parsed.from_address,
            reply_to_address=parsed.reply_to_address,
            subject=parsed.subject,
            text_body=parsed.text_body,
            html_body=parsed.html_body,
            received_at=at,
            sent_at=parsed.sent_at,
            imported_at=imported_at,
            viewed_at=imported_at,
            raw_s3_key=raw_key,
            raw_sha256=parsed.raw_sha256,
            # UNKNOWN is intentional. Sanitized text is readable; attachment bytes stay locked.
            is_suspicious=False,
            is_quarantined=False,
            automation_headers=parsed.automation_headers,
        )
        for kind, addresses in (
            ("TO", parsed.to_addresses),
            ("CC", parsed.cc_addresses),
            ("BCC", parsed.bcc_addresses),
        ):
            for address in dict.fromkeys(addresses):
                MessageRecipient.objects.create(
                    domain=transfer.domain,
                    message=message,
                    kind=kind,
                    address=address,
                    is_routing_recipient=direction == "INBOUND"
                    and address.rpartition("@")[2] == transfer.domain.hostname,
                )
        for kind, values in (
            ("MESSAGE_ID", [parsed.rfc_message_id] if parsed.rfc_message_id else []),
            ("REFERENCE", parsed.references),
            ("IN_REPLY_TO", parsed.in_reply_to),
        ):
            MessageReference.objects.bulk_create(
                [
                    MessageReference(
                        domain=transfer.domain,
                        message=message,
                        kind=kind,
                        position=n,
                        value_hash=reference_hash(value),
                    )
                    for n, value in enumerate(values[:65535])
                ],
                ignore_conflicts=True,
            )
        policy, _ = RetentionPolicy.objects.get_or_create(domain=transfer.domain)
        expires = imported_at + timedelta(
            days=_effective_retention_days(transfer.domain, policy, "attachment_days")
        )
        Attachment.objects.bulk_create(
            [
                Attachment(
                    domain=transfer.domain,
                    message=message,
                    display_name=attachment.filename,
                    content_type=attachment.content_type,
                    size=len(attachment.content),
                    sha256=attachment.sha256,
                    s3_key=attachment_key,
                    scan_status=Attachment.ScanStatus.UNKNOWN,
                    purge_at=expires,
                )
                for attachment, attachment_key in zip(
                    parsed.attachments, attachment_keys, strict=True
                )
            ]
        )
        _times(conversation)
        item.message = message
        item.status = MailTransferItem.Status.IMPORTED
        item.save(update_fields=("message", "status"))


def _thread(transfer, item):
    """Resolve late parents without merging or changing pre-existing conversations."""
    message = item.message
    if message is None:
        return
    headers = item.headers
    candidate = message
    visited = {message.id}
    for _ in range(200):
        values = list(reversed(headers.get("references", []))) + list(
            reversed(headers.get("in_reply_to", []))
        )
        parent = None
        participants = {
            headers.get("from"),
            *headers.get("to", []),
            *headers.get("cc", []),
            *headers.get("bcc", []),
        } - {None, ""}
        for value in values:
            matches = (
                Message.objects.filter(
                    domain=transfer.domain,
                    references__kind="MESSAGE_ID",
                    references__value_hash=reference_hash(value),
                )
                .exclude(id__in=visited)
                .select_related("conversation")
            )
            parent = next(
                (
                    m
                    for m in matches
                    if participants
                    & ({m.from_address} | set(m.recipients.values_list("address", flat=True)))
                ),
                None,
            )
            if parent:
                break
        if parent is None:
            break
        candidate = parent
        visited.add(parent.id)
        parent_item = transfer.items.filter(message=parent, status="IMPORTED").first()
        if parent_item is None:
            break
        headers = parent_item.headers
    if candidate.conversation_id == message.conversation_id:
        return
    with locked_transfer(transfer):
        message.refresh_from_db()
        source = Conversation.objects.select_for_update().get(id=message.conversation_id)
        target = Conversation.objects.select_for_update().get(id=candidate.conversation_id)
        _invalidate(source)
        _invalidate(target)
        message.conversation = target
        message.save(update_fields=("conversation", "updated_at"))
        ReplyDraft.objects.filter(domain=transfer.domain, context_message=message).update(
            conversation=target, is_stale=True
        )
        OutboundMessage.objects.filter(
            domain=transfer.domain, revision__draft__context_message=message
        ).update(conversation=target)
        _times(target)
        _times(source)
        if (
            not source.messages.exists()
            and not source.reply_drafts.exists()
            and not source.outbound_messages.exists()
        ):
            source.delete()


def import_batch(transfer, client):
    if transfer.phase == "index":
        _index(transfer, client)
        return
    deadline = time.monotonic() + 20
    if transfer.phase == "import":
        items = list(
            transfer.items.filter(status="PENDING").order_by("message_at", "position")[:BATCH_SIZE]
        )
        if not items:
            with locked_transfer(transfer) as current:
                current.phase = "thread"
                current.checkpoint = {"position": 0}
                current.save(update_fields=("phase", "checkpoint"))
            return
        for item in items:
            try:
                _store(transfer, item, client)
            except ValidationError:
                with locked_transfer(transfer):
                    item.status = "FAILED"
                    item.error_code = "invalid_mime"
                    item.save(update_fields=("status", "error_code"))
            if time.monotonic() >= deadline:
                break
    else:
        items = list(
            transfer.items.filter(
                status="IMPORTED", position__gt=transfer.checkpoint.get("position", 0)
            ).select_related("message")[:BATCH_SIZE]
        )
        if not items:
            finish(transfer)
            return
        for item in items:
            _thread(transfer, item)
            with locked_transfer(transfer) as current:
                current.checkpoint = {"position": item.position}
                current.save(update_fields=("checkpoint",))
            if time.monotonic() >= deadline:
                break
