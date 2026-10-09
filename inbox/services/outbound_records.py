from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from inbox.models import Message, MessageRecipient, MessageReference, OutboundMessage
from inbox.services.threading import reconcile_outbound_replies, reference_hash


@transaction.atomic
def record_outbound_message(
    outbound: OutboundMessage, *, requested_message_id: str = "", sent_at=None
) -> OutboundMessage:
    """Record provider-proven mail and reconcile early replies, including late SES events."""
    from inbox.services.outbound import get_outbound_control

    get_outbound_control(outbound.domain.owner, lock=True)
    current = (
        OutboundMessage.objects.select_for_update(of=("self",))
        .select_related("domain", "conversation")
        .get(id=outbound.id)
    )
    if not current.provider_message_id:
        return current
    outbound_at = sent_at or current.accepted_at or timezone.now()
    message, _ = Message.objects.get_or_create(
        domain=current.domain,
        provider_message_id=current.provider_message_id,
        defaults={
            "conversation": current.conversation,
            "direction": Message.Direction.OUTBOUND,
            "rfc_message_id": current.rfc_message_id,
            "from_address": current.from_address,
            "subject": current.subject,
            "text_body": current.body_text,
            "sent_at": outbound_at,
            "received_at": outbound_at,
        },
    )
    MessageRecipient.objects.get_or_create(
        domain=current.domain,
        message=message,
        kind=MessageRecipient.Kind.TO,
        address=current.to_address,
    )
    for identifier in {requested_message_id, current.rfc_message_id} - {""}:
        MessageReference.objects.get_or_create(
            domain=current.domain,
            message=message,
            kind=MessageReference.Kind.MESSAGE_ID,
            position=0,
            value_hash=reference_hash(identifier),
        )
    reconcile_outbound_replies(current)
    if current.status in {OutboundMessage.Status.ACCEPTED, OutboundMessage.Status.DELIVERED}:
        conversation = current.conversation.__class__.objects.select_for_update().get(
            id=current.conversation_id
        )
        conversation.last_outbound_at = max(
            conversation.last_outbound_at or outbound_at, outbound_at
        )
        conversation.last_message_at = max(conversation.last_message_at, outbound_at)
        conversation.save(update_fields=("last_outbound_at", "last_message_at", "updated_at"))
    return current
