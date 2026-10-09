from __future__ import annotations

import hashlib
import re

from django.db import transaction
from django.db.models import QuerySet

from inbox.models import (
    Conversation,
    ConversationTag,
    Domain,
    Message,
    MessageReference,
    Notification,
    OutboundMessage,
    ReplyDraft,
    ReportItem,
)
from inbox.services.mime import ParsedMIME

PREFIX_RE = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


def normalize_subject(value: str) -> str:
    return " ".join(PREFIX_RE.sub("", value).casefold().split())[:998]


def reference_hash(value: str) -> str:
    return hashlib.sha256(value.strip().casefold().encode()).hexdigest()


def ses_reference_id(value: str) -> str | None:
    """Extract the SES-assigned ID from a wire Message-ID, without guessing its region."""
    value = value.strip()
    if len(value) > 998:
        return None
    value = value.removeprefix("<").removesuffix(">")
    local, separator, hostname = value.rpartition("@")
    if (
        not separator
        or not local
        or len(local) > 160
        or len(hostname) > 253
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        return None
    if hostname.casefold().endswith(".amazonses.com"):
        return local
    return None


def reference_hashes(value: str) -> tuple[str, ...]:
    provider_id = ses_reference_id(value)
    if provider_id:
        return reference_hash(value), reference_hash(f"ses:{provider_id}")
    return (reference_hash(value),)


def record_ses_reference(outbound: OutboundMessage, identifier: str) -> None:
    """Store an observed full wire ID once it matches the authoritative SES send ID."""
    provider_id = ses_reference_id(identifier)
    if not provider_id or provider_id.casefold() != outbound.provider_message_id.casefold():
        return
    OutboundMessage.objects.filter(id=outbound.id).update(rfc_message_id=identifier)
    messages = Message.objects.filter(
        domain=outbound.domain,
        provider_message_id=outbound.provider_message_id,
        direction=Message.Direction.OUTBOUND,
    )
    messages.update(rfc_message_id=identifier)
    for message in messages:
        MessageReference.objects.get_or_create(
            domain=outbound.domain,
            message=message,
            kind=MessageReference.Kind.MESSAGE_ID,
            position=0,
            value_hash=reference_hash(identifier),
        )


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
                if candidate.direction == Message.Direction.OUTBOUND and ses_reference_id(
                    identifier
                ):
                    outbound = OutboundMessage.objects.filter(
                        domain=domain, provider_message_id=candidate.provider_message_id
                    ).first()
                    if outbound:
                        record_ses_reference(outbound, identifier)
                return candidate.conversation
            # A reply can arrive while SES is returning, before the timeline row exists.
            outbound = (
                OutboundMessage.objects.filter(domain=domain, rfc_message_id__iexact=identifier)
                .exclude(status=OutboundMessage.Status.FAILED)
                .select_related("conversation")
                .first()
            )
            provider_id = ses_reference_id(identifier)
            if outbound is None and provider_id:
                outbound = (
                    OutboundMessage.objects.filter(
                        domain=domain, provider_message_id__iexact=provider_id
                    )
                    .exclude(status=OutboundMessage.Status.FAILED)
                    .select_related("conversation")
                    .first()
                )
            if outbound and incoming_participants & {
                outbound.from_address.casefold(),
                outbound.to_address.casefold(),
            }:
                record_ses_reference(outbound, identifier)
                return outbound.conversation
    return None


@transaction.atomic
def reconcile_outbound_replies(outbound: OutboundMessage) -> int:
    """Join replies received before either the SES response or its tagged event was stored."""
    if not outbound.provider_message_id:
        return 0
    candidates = (
        Message.objects.filter(
            domain=outbound.domain,
            direction=Message.Direction.INBOUND,
            references__kind__in=[
                MessageReference.Kind.IN_REPLY_TO,
                MessageReference.Kind.REFERENCE,
            ],
            references__value_hash=reference_hash(f"ses:{outbound.provider_message_id}"),
        )
        .exclude(conversation=outbound.conversation)
        .select_related("conversation")
        .distinct()
    )
    participants = {outbound.from_address.casefold(), outbound.to_address.casefold()}
    source_ids = {
        message.conversation_id
        for message in candidates
        if participants & _conversation_participants(message.conversation)
    }
    if not source_ids:
        return 0
    timeline = Message.objects.filter(
        domain=outbound.domain,
        provider_message_id=outbound.provider_message_id,
        direction=Message.Direction.OUTBOUND,
    ).first()
    if timeline:
        for incoming in candidates:
            if incoming.conversation_id not in source_ids:
                continue
            for reference in incoming.references.filter(
                value_hash=reference_hash(f"ses:{outbound.provider_message_id}")
            ):
                for value_hash in (
                    incoming.references.filter(kind=reference.kind, position=reference.position)
                    .exclude(id=reference.id)
                    .values_list("value_hash", flat=True)
                ):
                    MessageReference.objects.get_or_create(
                        domain=outbound.domain,
                        message=timeline,
                        kind=MessageReference.Kind.MESSAGE_ID,
                        position=0,
                        value_hash=value_hash,
                    )
    locked = {
        conversation.id: conversation
        for conversation in Conversation.objects.select_for_update()
        .filter(domain=outbound.domain, id__in=source_ids | {outbound.conversation_id})
        .order_by("id")
    }
    target = locked[outbound.conversation_id]
    moved = 0
    for source_id in source_ids:
        source = locked.get(source_id)
        if source is None:
            continue
        moved += Message.objects.filter(conversation=source).update(conversation=target)
        for model in (OutboundMessage, ReplyDraft, ReportItem, Notification):
            model.objects.filter(domain=outbound.domain, conversation=source).update(
                conversation=target
            )
        for tag in source.tags.all():
            ConversationTag.objects.get_or_create(
                domain=outbound.domain,
                conversation=target,
                normalized_name=tag.normalized_name,
                defaults={"name": tag.name},
            )
        Conversation.objects.filter(merge_suggestion=source).exclude(id=target.id).update(
            merge_suggestion=target
        )
        target.first_message_at = min(target.first_message_at, source.first_message_at)
        target.last_message_at = max(target.last_message_at, source.last_message_at)
        for name in ("last_inbound_at", "last_outbound_at"):
            at = getattr(source, name)
            if at is not None:
                setattr(target, name, max(getattr(target, name) or at, at))
        target.starred_at = target.starred_at or source.starred_at
        if target.merge_suggestion_id == source.id:
            target.merge_suggestion = None
        source.delete()
    target.archived_at = None
    target.trashed_at = None
    target.save()
    return moved


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
