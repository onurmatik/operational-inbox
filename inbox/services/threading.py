from __future__ import annotations

import hashlib
import re

from django.db.models import QuerySet

from inbox.models import Conversation, Domain, Message, MessageReference
from inbox.services.mime import ParsedMIME

PREFIX_RE = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


def normalize_subject(value: str) -> str:
    return " ".join(PREFIX_RE.sub("", value).casefold().split())[:998]


def reference_hash(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode()).hexdigest()


def _conversation_participants(conversation: Conversation) -> set[str]:
    senders = set(conversation.messages.values_list("from_address", flat=True))
    recipients = set(
        conversation.messages.values_list("recipients__address", flat=True).exclude(
            recipients__address__isnull=True
        )
    )
    return {address.casefold() for address in senders | recipients if address}


def _candidate_messages(domain: Domain, ids: list[str]) -> QuerySet[Message]:
    hashes = [reference_hash(value) for value in ids]
    return (
        Message.objects.filter(domain=domain, references__value_hash__in=hashes)
        .select_related("conversation")
        .distinct()
    )


def match_conversation(
    *, domain: Domain, parsed: ParsedMIME, envelope_recipients: list[str]
) -> Conversation | None:
    incoming_participants = {
        parsed.from_address.casefold(),
        *[address.casefold() for address in envelope_recipients],
    }
    for identifiers in (list(reversed(parsed.references)), list(reversed(parsed.in_reply_to))):
        if not identifiers:
            continue
        by_message_id = {
            reference.value_hash: reference.message
            for reference in MessageReference.objects.filter(
                message__domain=domain,
                kind=MessageReference.Kind.MESSAGE_ID,
                value_hash__in=[reference_hash(item) for item in identifiers],
            ).select_related("message__conversation")
        }
        for identifier in identifiers:
            candidate = by_message_id.get(reference_hash(identifier))
            if candidate and incoming_participants & _conversation_participants(
                candidate.conversation
            ):
                return candidate.conversation
    return None


def merge_suggestion(domain: Domain, subject: str, exclude: Conversation) -> Conversation | None:
    normalized = normalize_subject(subject)
    if not normalized:
        return None
    return (
        Conversation.objects.filter(domain=domain, normalized_subject=normalized)
        .exclude(id=exclude.id)
        .order_by("-last_message_at")
        .first()
    )
