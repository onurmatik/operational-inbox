from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from inbox.models import Domain, Message, Notification, OutboundMessage


def _email_notification(
    *,
    domain: Domain,
    dedupe_key: str,
    kind: str,
    title: str,
    body: str,
    target_path: str,
    conversation=None,
) -> list[Notification]:
    notification, _ = Notification.objects.get_or_create(
        domain=domain,
        channel=Notification.Channel.EMAIL,
        dedupe_key=dedupe_key,
        defaults={
            "conversation": conversation,
            "kind": kind,
            "title": title,
            "body": f"{body}\n\n{settings.PUBLIC_BASE_URL.rstrip('/')}{target_path}",
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
        target_path=(
            reverse("conversation_detail", args=[message.conversation_id])
            + f"#message-{message.id}"
        ),
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
        target_path=reverse("domain_detail", args=[domain.id]),
        title="Domain DNS drift detected",
        body="A required DNS record no longer matches. Inbound readiness is degraded.",
    )


def create_outbound_problem_notifications(
    outbound: OutboundMessage,
) -> list[Notification]:
    if outbound.status not in {
        OutboundMessage.Status.FAILED,
        OutboundMessage.Status.UNKNOWN,
        OutboundMessage.Status.BOUNCED,
        OutboundMessage.Status.COMPLAINED,
    }:
        return []
    label = outbound.get_status_display().lower()
    return _email_notification(
        domain=outbound.domain,
        conversation=outbound.conversation,
        dedupe_key=f"outbound:{outbound.id}:{outbound.status}",
        kind="outbound_problem",
        target_path=(
            reverse("conversation_detail", args=[outbound.conversation_id])
            + f"#outbound-{outbound.id}"
        ),
        title=f"Outbound reply {label}",
        body=(
            f"A reply to {outbound.to_address} is {label}. "
            "Open the message to inspect the attempt before deciding whether to resend."
        ),
    )


@transaction.atomic
def send_pending_email_notification(notification: Notification) -> bool:
    # Serialize delivery with review: always lock message before notification.
    # Refresh the notification so queued jobs cannot send a resolved alert.
    if notification.kind == "security_verdict":
        message_id = notification.dedupe_key.removeprefix("security:")
        Message.objects.select_for_update().filter(
            domain_id=notification.domain_id, id=message_id
        ).first()
    notification = Notification.objects.select_for_update().get(id=notification.id)
    if notification.channel != Notification.Channel.EMAIL:
        return False
    if notification.status in {Notification.Status.SENT, Notification.Status.RESOLVED}:
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
