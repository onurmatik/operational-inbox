from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from inbox.models import DigestDelivery, DigestSettings, Message
from inbox.services.tenancy import owned_domains


def _scheduled_on(preference: DigestSettings, day: date) -> datetime:
    # fold=0 chooses the first occurrence of an ambiguous clock time. Converting
    # to UTC also moves nonexistent spring-forward times past the clock change.
    return datetime.combine(
        day, preference.send_time, tzinfo=ZoneInfo(preference.timezone_name)
    ).astimezone(UTC)


def next_digest_at(preference: DigestSettings, after: datetime) -> datetime:
    day = after.astimezone(ZoneInfo(preference.timezone_name)).date()
    candidate = _scheduled_on(preference, day)
    if candidate <= after:
        candidate = _scheduled_on(preference, day + timedelta(days=1))
    return candidate


def schedule_digests(now: datetime | None = None) -> int:
    from inbox.services.jobs import enqueue_job

    now = now or timezone.now()
    count = 0
    due_ids = DigestSettings.objects.filter(
        enabled=True,
        next_send_at__lte=now,
        user__is_active=True,
        user__email_verified_at__isnull=False,
    ).values_list("id", flat=True)
    for preference_id in list(due_ids):
        with transaction.atomic():
            preference = DigestSettings.objects.select_for_update().get(id=preference_id)
            if (
                not preference.enabled
                or not preference.next_send_at
                or preference.next_send_at > now
            ):
                continue
            # After downtime send the latest scheduled window, not a burst of
            # old daily emails. Retries retain this exact window.
            local_day = now.astimezone(ZoneInfo(preference.timezone_name)).date()
            end = _scheduled_on(preference, local_day)
            if end > now:
                end = _scheduled_on(preference, local_day - timedelta(days=1))
            delivery, _ = DigestDelivery.objects.get_or_create(
                preference=preference,
                period_end=end,
                defaults={
                    "period_start": end - timedelta(hours=preference.window_hours),
                    "timezone_name": preference.timezone_name,
                },
            )
            enqueue_job(
                kind="send_digest",
                idempotency_key=f"digest:{delivery.id}",
                payload={"delivery_id": str(delivery.id)},
                due_at=now,
            )
            preference.next_send_at = next_digest_at(preference, now)
            preference.save(update_fields=("next_send_at", "updated_at"))
            count += 1
    return count


def _compact(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


@transaction.atomic
def send_digest(delivery_id: str) -> None:
    preference_id = (
        DigestDelivery.objects.filter(id=delivery_id)
        .values_list("preference_id", flat=True)
        .first()
    )
    if preference_id is None:
        return
    # Use the same lock as settings updates and scheduling so opt-out and
    # duplicate workers cannot race a delivery.
    preference = (
        DigestSettings.objects.select_for_update().select_related("user").get(id=preference_id)
    )
    delivery = DigestDelivery.objects.select_for_update().get(id=delivery_id)
    if delivery.status != DigestDelivery.Status.PENDING:
        return
    if (
        not preference.enabled
        or not preference.user.is_active
        or not preference.user.is_email_verified
    ):
        delivery.status = DigestDelivery.Status.SKIPPED
        delivery.save(update_fields=("status", "updated_at"))
        return

    zone = ZoneInfo(delivery.timezone_name)
    incoming = list(
        Message.objects.filter(
            domain__in=owned_domains(preference.user),
            direction=Message.Direction.INBOUND,
            received_at__gte=delivery.period_start,
            received_at__lt=delivery.period_end,
        )
        .select_related("domain")
        .order_by("domain__hostname", "received_at", "id")
    )
    delivery.message_count = len(incoming)
    if not incoming:
        delivery.status = DigestDelivery.Status.SKIPPED
        delivery.save(update_fields=("status", "message_count", "updated_at"))
        return
    lines = [
        "Digest",
        f"{delivery.period_start.astimezone(zone):%Y-%m-%d %H:%M %z} - "
        f"{delivery.period_end.astimezone(zone):%Y-%m-%d %H:%M %z} ({delivery.timezone_name})",
        f"{len(incoming)} incoming message(s) across all your domains.",
        "",
    ]
    hostname = None
    for message in incoming:
        if hostname != message.domain.hostname:
            hostname = message.domain.hostname
            lines.extend([hostname, ""])
        lines.append(f"{_compact(message.subject, 160) or '(No subject)'}")
        lines.append(
            f"From: {_compact(message.from_address, 320)} · "
            f"{message.received_at.astimezone(zone):%Y-%m-%d %H:%M}"
        )
        if message.is_quarantined:
            preview = "Quarantined — open the conversation for review."
        elif message.normalized_purged_at:
            preview = "Message content expired."
        else:
            preview = _compact(message.text_body or strip_tags(message.html_body), 240)
            if message.is_suspicious:
                preview = "Suspicious message. " + preview
        if preview:
            lines.append(preview)
        path = reverse("conversation_detail", kwargs={"conversation_id": message.conversation_id})
        lines.extend([f"{settings.PUBLIC_BASE_URL}{path}", ""])
    lines.append(f"Manage digest: {settings.PUBLIC_BASE_URL}{reverse('settings')}")
    sent = send_mail(
        subject=f"Digest · {len(incoming)} incoming message(s)",
        message="\n".join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[preference.user.email],
        fail_silently=False,
    )
    if sent != 1:
        raise RuntimeError("Digest email was not accepted for delivery.")
    delivery.status = DigestDelivery.Status.SENT
    delivery.sent_at = timezone.now()
    delivery.save(update_fields=("status", "sent_at", "message_count", "updated_at"))
