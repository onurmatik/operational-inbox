from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from inbox.models import Conversation, ConversationTag


def normalize_tag(value: str) -> tuple[str, str]:
    name = " ".join(value.strip().removeprefix("#").split())
    normalized_name = name.casefold()
    if not name:
        raise ValidationError("Enter a tag.")
    if len(name) > 64 or len(normalized_name) > 64:
        raise ValidationError("Tags must be 64 characters or fewer.")
    return name, normalized_name


@transaction.atomic
def add_conversation_tag(
    conversation: Conversation,
    value: str,
    *,
    agent_action: bool = False,
) -> tuple[ConversationTag, bool]:
    if not agent_action:
        from inbox.services.outbound import get_outbound_control

        get_outbound_control(conversation.domain.owner, lock=True)
    name, normalized_name = normalize_tag(value)
    tag, created = ConversationTag.objects.get_or_create(
        domain=conversation.domain,
        conversation=conversation,
        normalized_name=normalized_name,
        defaults={"name": name},
    )

    if created and not agent_action:
        from inbox.services.handling import interrupt_handling

        interrupt_handling(conversation, takeover=False)
    return tag, created


@transaction.atomic
def remove_conversation_tag(
    conversation: Conversation,
    value: str,
    *,
    agent_action: bool = False,
) -> ConversationTag | None:
    if not agent_action:
        from inbox.services.outbound import get_outbound_control

        get_outbound_control(conversation.domain.owner, lock=True)
    _, normalized_name = normalize_tag(value)
    tag = ConversationTag.objects.filter(
        domain=conversation.domain,
        conversation=conversation,
        normalized_name=normalized_name,
    ).first()
    if tag is not None:
        tag.delete()
        if not agent_action:
            from inbox.services.handling import interrupt_handling

            interrupt_handling(conversation, takeover=False)
    return tag
