from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils import timezone

from inbox.models import Conversation, Message


@dataclass(frozen=True)
class ConversationActionResult:
    state_changed: bool
    viewed_messages: int = 0

    @property
    def changed(self) -> bool:
        return self.state_changed or bool(self.viewed_messages)


def mark_conversation_viewed(conversation: Conversation) -> int:
    now = timezone.now()
    return Message.objects.filter(
        domain=conversation.domain,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        viewed_at__isnull=True,
    ).update(viewed_at=now, updated_at=now)


def apply_conversation_action(
    conversation: Conversation,
    action: str,
) -> ConversationActionResult:
    now = timezone.now()
    update_fields: list[str] = []
    viewed_messages = 0

    if action == "star":
        if conversation.starred_at is None:
            conversation.starred_at = now
            update_fields.append("starred_at")
    elif action == "unstar":
        if conversation.starred_at is not None:
            conversation.starred_at = None
            update_fields.append("starred_at")
    elif action == "archive":
        if conversation.archived_at is None or conversation.trashed_at is not None:
            conversation.archived_at = now
            conversation.trashed_at = None
            update_fields.extend(("archived_at", "trashed_at"))
    elif action == "trash":
        if conversation.trashed_at is None or conversation.archived_at is not None:
            conversation.trashed_at = now
            conversation.archived_at = None
            update_fields.extend(("trashed_at", "archived_at"))
    elif action == "restore":
        if conversation.archived_at is not None or conversation.trashed_at is not None:
            conversation.archived_at = None
            conversation.trashed_at = None
            update_fields.extend(("archived_at", "trashed_at"))
    else:
        raise ValidationError("Select a supported conversation action.")

    if update_fields:
        conversation.save(update_fields=(*dict.fromkeys(update_fields), "updated_at"))
    return ConversationActionResult(
        state_changed=bool(update_fields),
        viewed_messages=viewed_messages,
    )
