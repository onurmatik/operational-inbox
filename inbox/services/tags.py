from __future__ import annotations

from django.core.exceptions import ValidationError

from inbox.models import Conversation, ConversationTag


def normalize_tag(value: str) -> tuple[str, str]:
    name = " ".join(value.strip().removeprefix("#").split())
    normalized_name = name.casefold()
    if not name:
        raise ValidationError("Enter a tag.")
    if len(name) > 64 or len(normalized_name) > 64:
        raise ValidationError("Tags must be 64 characters or fewer.")
    return name, normalized_name


def add_conversation_tag(
    conversation: Conversation,
    value: str,
) -> tuple[ConversationTag, bool]:
    name, normalized_name = normalize_tag(value)
    return ConversationTag.objects.get_or_create(
        domain=conversation.domain,
        conversation=conversation,
        normalized_name=normalized_name,
        defaults={"name": name},
    )


def remove_conversation_tag(
    conversation: Conversation,
    value: str,
) -> ConversationTag | None:
    _, normalized_name = normalize_tag(value)
    tag = ConversationTag.objects.filter(
        domain=conversation.domain,
        conversation=conversation,
        normalized_name=normalized_name,
    ).first()
    if tag is not None:
        tag.delete()
    return tag
