from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from inbox.models import Classification, Conversation, Domain, Message, Notification


def _paired_notifications(
    *, domain: Domain, dedupe_key: str, kind: str, title: str, body: str, conversation=None
) -> list[Notification]:
    result: list[Notification] = []
    for channel in (Notification.Channel.IN_APP, Notification.Channel.EMAIL):
        notification, _ = Notification.objects.get_or_create(
            domain=domain,
            channel=channel,
            dedupe_key=dedupe_key,
            defaults={
                "conversation": conversation,
                "kind": kind,
                "title": title,
                "body": body,
            },
        )
        result.append(notification)
    return result


def create_classification_notifications(classification: Classification) -> list[Notification]:
    if classification.category not in {
        Classification.Category.ACTIONABLE,
        Classification.Category.SUSPICIOUS,
    } and classification.urgency not in {
        Classification.Urgency.HIGH,
        Classification.Urgency.CRITICAL,
    }:
        return []
    message = classification.message
    return _paired_notifications(
        domain=message.domain,
        conversation=message.conversation,
        dedupe_key=f"classification:{classification.id}",
        kind="important_classification",
        title=f"{classification.get_urgency_display()}: {message.subject or '(no subject)'}",
        body=classification.summary or "An operational message needs review.",
    )


def create_security_notifications(message: Message) -> list[Notification]:
    if not message.is_quarantined and not message.is_suspicious:
        return []
    return _paired_notifications(
        domain=message.domain,
        conversation=message.conversation,
        dedupe_key=f"security:{message.id}",
        kind="security_verdict",
        title=(
            "Quarantined inbound message"
            if message.is_quarantined
            else "Suspicious inbound message"
        ),
        body=(
            "A message failed the malware verdict and is locked for review."
            if message.is_quarantined
            else "An authentication or spam verdict requires owner review."
        ),
    )


def create_aging_notifications(domain: Domain, *, now=None) -> list[Notification]:
    now = now or timezone.now()
    schedule = domain.report_schedule
    cutoff = now - timedelta(hours=schedule.aging_reminder_hours)
    try:
        domain_timezone = ZoneInfo(domain.timezone)
    except ZoneInfoNotFoundError:
        domain_timezone = ZoneInfo("UTC")
    local_date = timezone.localdate(now, timezone=domain_timezone)
    created: list[Notification] = []
    for conversation in Conversation.objects.filter(
        domain=domain,
        status__in=[Conversation.Status.OPEN, Conversation.Status.WAITING_EXTERNAL],
        last_message_at__lte=cutoff,
    ):
        created.extend(
            _paired_notifications(
                domain=domain,
                conversation=conversation,
                dedupe_key=f"aging:{conversation.id}:{local_date.isoformat()}",
                kind="aging_conversation",
                title="Conversation needs review",
                body=(
                    "This unresolved conversation has been waiting at least "
                    f"{schedule.aging_reminder_hours} hours."
                ),
            )
        )
    return created


def create_domain_drift_notifications(domain: Domain) -> list[Notification]:
    return _paired_notifications(
        domain=domain,
        dedupe_key=f"domain-drift:{domain.id}:{domain.last_checked_at:%Y%m%d%H}",
        kind="domain_drift",
        title="Domain DNS drift detected",
        body="A required DNS record no longer matches. Inbound readiness is degraded.",
    )


def send_pending_email_notification(notification: Notification) -> bool:
    if notification.channel != Notification.Channel.EMAIL:
        return False
    if notification.status == Notification.Status.SENT:
        return True
    try:
        send_mail(
            subject=notification.title,
            message=notification.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[notification.domain.owner.email],
            fail_silently=False,
        )
    except Exception:
        notification.status = Notification.Status.FAILED
        notification.error_code = "email_delivery_failed"
        notification.save(update_fields=("status", "error_code", "updated_at"))
        return False
    notification.status = Notification.Status.SENT
    notification.sent_at = timezone.now()
    notification.save(update_fields=("status", "sent_at", "updated_at"))
    return True
