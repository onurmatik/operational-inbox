from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from inbox.models import Message, Notification

SPAM_REVIEW_EVENTS = {
    "not_spam": "message.marked_not_spam",
    "undo": "message.not_spam_undone",
}


def apply_spam_review(message: Message, action: str) -> bool:
    """Caller must lock the message and write the audit in the same transaction."""
    if action not in SPAM_REVIEW_EVENTS:
        raise ValidationError("Select a supported spam review action.")
    if message.direction != Message.Direction.INBOUND:
        raise ValidationError("Only inbound messages support spam review.")
    if action == "not_spam":
        if message.not_spam_at is not None:
            return False
        if not message.can_mark_not_spam:
            raise ValidationError("This message cannot be marked as not spam.")
        message.not_spam_at = timezone.now()
    else:
        if message.not_spam_at is None:
            return False
        message.not_spam_at = None

    message.is_suspicious = message.has_auth_failure or (
        message.spam_verdict != Message.Verdict.PASS and message.not_spam_at is None
    )
    message.save(update_fields=("not_spam_at", "is_suspicious", "updated_at"))
    notifications = Notification.objects.select_for_update().filter(
        domain=message.domain,
        kind="security_verdict",
        dedupe_key=f"security:{message.id}",
        channel=Notification.Channel.EMAIL,
    )
    now = timezone.now()
    if not message.is_suspicious and not message.is_quarantined:
        notifications.filter(
            status__in=(Notification.Status.PENDING, Notification.Status.FAILED)
        ).update(status=Notification.Status.RESOLVED, error_code="", updated_at=now)
    elif action == "undo":
        for notification in notifications.filter(status=Notification.Status.RESOLVED):
            notification.status = Notification.Status.PENDING
            notification.save(update_fields=("status", "updated_at"))
    return True


def spam_review_metadata(message: Message, action: str) -> dict:
    return {
        "action": action,
        "not_spam_at": message.not_spam_at.isoformat() if message.not_spam_at else None,
        "is_suspicious": message.is_suspicious,
        "is_quarantined": message.is_quarantined,
        "spam_verdict": message.spam_verdict,
        "virus_verdict": message.virus_verdict,
        "spf_verdict": message.spf_verdict,
        "dkim_verdict": message.dkim_verdict,
        "dmarc_verdict": message.dmarc_verdict,
    }
