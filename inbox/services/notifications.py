from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from inbox.models import Domain, Message, Notification


def _email_notification(
    *, domain: Domain, dedupe_key: str, kind: str, title: str, body: str, conversation=None
) -> list[Notification]:
    notification, _ = Notification.objects.get_or_create(
        domain=domain,
        channel=Notification.Channel.EMAIL,
        dedupe_key=dedupe_key,
        defaults={
            "conversation": conversation,
            "kind": kind,
            "title": title,
            "body": body,
        },
    )
    return [notification]


def create_security_notifications(message: Message) -> list[Notification]:
    if not message.is_quarantined and not message.is_suspicious:
        return []
    return _email_notification(
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


def create_domain_drift_notifications(domain: Domain) -> list[Notification]:
    return _email_notification(
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
