from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from inbox.models import Classification, Conversation, Domain, Message, Notification, Organization


def _paired_notifications(
    *,
    organization: Organization,
    dedupe_key: str,
    kind: str,
    title: str,
    body: str,
    project=None,
    conversation=None,
) -> list[Notification]:
    result: list[Notification] = []
    for channel in (Notification.Channel.IN_APP, Notification.Channel.EMAIL):
        notification, _ = Notification.objects.get_or_create(
            organization=organization,
            channel=channel,
            dedupe_key=dedupe_key,
            defaults={
                "project": project,
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
    dedupe = f"classification:{classification.id}"
    title = f"{classification.get_urgency_display()}: {message.subject or '(no subject)'}"
    body = classification.summary or "An operational message needs review."
    return _paired_notifications(
        organization=message.organization,
        project=message.project,
        conversation=message.conversation,
        dedupe_key=dedupe,
        kind="important_classification",
        title=title,
        body=body,
    )


def create_security_notifications(message: Message) -> list[Notification]:
    if not message.is_quarantined and not message.is_suspicious:
        return []
    title = (
        "Quarantined inbound message" if message.is_quarantined else "Suspicious inbound message"
    )
    body = (
        "A message failed the malware verdict and is locked for review."
        if message.is_quarantined
        else "An authentication or spam verdict requires owner review."
    )
    return _paired_notifications(
        organization=message.organization,
        project=message.project,
        conversation=message.conversation,
        dedupe_key=f"security:{message.id}",
        kind="security_verdict",
        title=title,
        body=body,
    )


def create_aging_notifications(organization: Organization, *, now=None) -> list[Notification]:
    now = now or timezone.now()
    schedule = organization.report_schedule
    cutoff = now - timedelta(hours=schedule.aging_reminder_hours)
    try:
        organization_timezone = ZoneInfo(organization.timezone)
    except ZoneInfoNotFoundError:
        organization_timezone = ZoneInfo("UTC")
    local_date = timezone.localdate(now, timezone=organization_timezone)
    created: list[Notification] = []
    for conversation in Conversation.objects.filter(
        organization=organization,
        status__in=[Conversation.Status.OPEN, Conversation.Status.WAITING_EXTERNAL],
        last_message_at__lte=cutoff,
    ):
        created.extend(
            _paired_notifications(
                organization=organization,
                project=conversation.project,
                conversation=conversation,
                dedupe_key=f"aging:{conversation.id}:{local_date.isoformat()}",
                kind="aging_conversation",
                title="Conversation needs review",
                body=(
                    f"This unresolved conversation has been waiting at least "
                    f"{schedule.aging_reminder_hours} hours."
                ),
            )
        )
    return created


def create_domain_drift_notifications(domain: Domain) -> list[Notification]:
    return _paired_notifications(
        organization=domain.organization,
        project=domain.project,
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
            recipient_list=[notification.organization.owner.email],
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
