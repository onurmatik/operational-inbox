from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.headerregistry import AddressHeader
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import PurePath

import nh3
from django.core.exceptions import ValidationError
from django.utils import timezone

MAX_MIME_BYTES = 40 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PARTS = 200
MAX_NESTING = 20

MESSAGE_ID_RE = re.compile(r"<[^<>\s]{1,990}>")
ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "td": {"colspan", "rowspan"}, "th": {"scope"}}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str
    content_type: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ParsedMIME:
    subject: str
    from_address: str
    reply_to_address: str
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    rfc_message_id: str
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    sent_at: datetime | None
    text_body: str
    html_body: str
    attachments: tuple[ParsedAttachment, ...]
    raw_sha256: str


def _addresses(message: Message, header_name: str) -> tuple[str, ...]:
    header = message[header_name]
    if isinstance(header, AddressHeader):
        return tuple(
            address.addr_spec.casefold()
            for address in header.addresses
            if address.addr_spec and "\r" not in address.addr_spec and "\n" not in address.addr_spec
        )
    return ()


def _safe_filename(value: str | None, index: int) -> str:
    name = PurePath((value or f"attachment-{index}").replace("\\", "/")).name
    name = "".join(character for character in name if character.isprintable())
    name = name.replace("\r", "").replace("\n", "").strip(" .")
    return (name or f"attachment-{index}")[:255]


def _decode_text(part: Message) -> str:
    decoded = part.get_payload(decode=True)
    payload = decoded if isinstance(decoded, bytes) else b""
    if len(payload) > MAX_BODY_BYTES:
        payload = payload[:MAX_BODY_BYTES]
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def sanitize_html(value: str) -> str:
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="nofollow noopener noreferrer",
        strip_comments=True,
    )


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return html.unescape(" ".join(" ".join(parser.parts).split()))


def parse_mime(raw: bytes) -> ParsedMIME:
    if len(raw) > MAX_MIME_BYTES:
        raise ValidationError("The MIME message exceeds the 40 MB ingestion limit.")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValidationError("The MIME message could not be parsed.") from exc

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[ParsedAttachment] = []
    part_count = 0
    stack: list[tuple[Message, int]] = [(message, 0)]
    while stack:
        part, depth = stack.pop()
        part_count += 1
        if part_count > MAX_PARTS or depth > MAX_NESTING:
            raise ValidationError("The MIME message has too many nested parts.")
        if part.is_multipart():
            children = list(part.iter_parts())  # type: ignore[attr-defined]
            stack.extend((child, depth + 1) for child in reversed(children))
            continue

        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_type = part.get_content_type().casefold()
        if disposition == "attachment" or filename:
            decoded = part.get_payload(decode=True)
            content = decoded if isinstance(decoded, bytes) else b""
            attachments.append(
                ParsedAttachment(
                    filename=_safe_filename(filename, len(attachments) + 1),
                    content_type=content_type,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
            continue
        if content_type == "text/plain":
            text_parts.append(_decode_text(part))
        elif content_type == "text/html":
            html_parts.append(_decode_text(part))

    safe_html = "\n".join(sanitize_html(part) for part in html_parts)
    text_body = "\n\n".join(text_parts).strip()
    if not text_body and safe_html:
        text_body = html_to_text(safe_html)
    date_header = str(message.get("Date", ""))
    sent_at = None
    if date_header:
        try:
            sent_at = parsedate_to_datetime(date_header)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.get_current_timezone())
        except (TypeError, ValueError, OverflowError):
            sent_at = None

    subject = str(message.get("Subject", "")).replace("\r", " ").replace("\n", " ")[:998]
    message_ids = MESSAGE_ID_RE.findall(str(message.get("Message-ID", "")))
    return ParsedMIME(
        subject=subject,
        from_address=next(iter(_addresses(message, "From")), "unknown@invalid.local")[:320],
        reply_to_address=next(iter(_addresses(message, "Reply-To")), "")[:320],
        to_addresses=_addresses(message, "To"),
        cc_addresses=_addresses(message, "Cc"),
        rfc_message_id=(message_ids[0] if message_ids else "")[:998],
        in_reply_to=tuple(MESSAGE_ID_RE.findall(str(message.get("In-Reply-To", "")))),
        references=tuple(MESSAGE_ID_RE.findall(str(message.get("References", "")))),
        sent_at=sent_at,
        text_body=text_body,
        html_body=safe_html,
        attachments=tuple(attachments),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
