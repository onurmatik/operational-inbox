"""Bounded mboxrd framing. Offsets always refer to the uploaded, immutable bytes."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import BinaryIO

from django.core.exceptions import ValidationError

from inbox.services.mime import MAX_MIME_BYTES


@dataclass(frozen=True)
class Entry:
    offset: int
    length: int
    envelope: bytes
    raw: bytes
    oversized: bool = False


class StreamingReader(io.RawIOBase):
    def __init__(self, body):
        self.body = body

    def readable(self):
        return True

    def readinto(self, target):
        data = self.body.read(len(target))
        target[: len(data)] = data
        return len(data)

    def close(self):
        self.body.close()
        super().close()


def entries(stream: BinaryIO, *, offset: int = 0):
    """Read at most one MIME message into memory, including overlong physical lines."""
    position = offset
    start = offset
    envelope = b""
    chunks: list[bytes] = []
    size = 0
    line_start = True
    while piece := stream.readline(64 * 1024):
        separator = line_start and piece.startswith(b"From ")
        if separator:
            if envelope:
                yield Entry(
                    start, position - start, envelope, b"".join(chunks), size > MAX_MIME_BYTES
                )
            if not piece.endswith(b"\n") or len(piece) > 4096:
                raise ValidationError("Invalid MBOX separator.", code="invalid_mbox")
            start, envelope, chunks, size = position, piece, [], 0
        elif not envelope:
            raise ValidationError("Choose an MBOX file.", code="invalid_mbox")
        else:
            size += len(piece)
            if size <= MAX_MIME_BYTES:
                chunks.append(piece)
            else:
                chunks.clear()
        position += len(piece)
        line_start = piece.endswith(b"\n")
    if envelope:
        yield Entry(start, position - start, envelope, b"".join(chunks), size > MAX_MIME_BYTES)


def decode_entry(raw: bytes) -> bytes:
    raw = raw.replace(b"\r\n", b"\n")
    headers, separator, body = raw.partition(b"\n\n")
    if not separator or not re.search(rb"(?im)^(from|date|subject|message-id):", headers):
        raise ValidationError("Invalid email headers.", code="invalid_mime")
    # One blank line belongs to the MBOX frame, not to the MIME message.
    if body.endswith(b"\n\n"):
        body = body[:-1]
    body = re.sub(rb"(?m)^>(>*From )", rb"\1", body)
    return headers + separator + body


def envelope_date(envelope: bytes) -> datetime | None:
    try:
        value = envelope.decode("ascii").strip().split(" ", 2)[2]
        at = parsedate_to_datetime(value)
        return at.replace(tzinfo=UTC) if at.tzinfo is None else at
    except (ValueError, IndexError, TypeError, OverflowError, UnicodeError):
        return None


def encode_entry(raw: bytes, *, sender: str, at: datetime) -> bytes:
    raw = raw.replace(b"\r\n", b"\n")
    headers, separator, body = raw.partition(b"\n\n")
    if not separator:
        raise ValidationError("Invalid MIME output.")
    body = re.sub(rb"(?m)^(>*From )", rb">\1", body)
    safe_sender = (
        sender if re.fullmatch(r"[^\s<>\x00-\x1f]+@[^\s<>\x00-\x1f]+", sender) else "MAILER-DAEMON"
    )
    stamp = at.astimezone(UTC).strftime("%a %b %d %H:%M:%S %Y")
    frame = f"From {safe_sender} {stamp}\n".encode("ascii", errors="replace")
    mime = headers + separator + body
    return frame + mime + (b"" if mime.endswith(b"\n") else b"\n") + b"\n"
