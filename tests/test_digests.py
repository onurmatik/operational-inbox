from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from inbox.forms import DigestSettingsForm
from inbox.models import (
    Conversation,
    DigestDelivery,
    DigestSettings,
    Domain,
    DurableJob,
    Message,
    User,
)
from inbox.services.digests import next_digest_at, schedule_digests, send_digest
from inbox.services.jobs import run_due_jobs, schedule_work

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fixed_clock():
    with freeze_time("2026-10-30T10:00:00+00:00"):
        yield


@pytest.fixture
def preference(owner):
    return DigestSettings.objects.create(
        user=owner, enabled=True, next_send_at=datetime(2026, 10, 30, 9, tzinfo=UTC)
    )


def add_message(domain, received_at, *, subject="Included", **kwargs):
    conversation = Conversation.objects.create(
        domain=domain, subject=subject, first_message_at=received_at, last_message_at=received_at
    )
    return Message.objects.create(
        domain=domain,
        conversation=conversation,
        received_at=received_at,
        direction=kwargs.pop("direction", Message.Direction.INBOUND),
        provider_message_id=str(conversation.id),
        from_address="sender@example.net",
        subject=subject,
        text_body="Message preview",
        **kwargs,
    )


def test_digest_all_domains_isolated_fixed_window_and_deduplicated(preference, domain, mailoutbox):
    end = preference.next_send_at
    second = Domain.objects.create(
        owner=preference.user,
        hostname="second.example.com",
        claim_expires_at=end + timedelta(days=1),
    )
    other_user = User.objects.create_user(email="other@example.com")
    other = Domain.objects.create(
        owner=other_user, hostname="other.example.com", claim_expires_at=end + timedelta(days=1)
    )
    included = add_message(domain, end - timedelta(hours=24), subject="Boundary start")
    add_message(second, end - timedelta(hours=1), subject="Second domain")
    add_message(other, end - timedelta(hours=1), subject="Other account secret")
    add_message(domain, end - timedelta(hours=25), subject="Too old")
    add_message(domain, end, subject="Boundary end")
    add_message(domain, end - timedelta(hours=1), subject="Outgoing", direction="OUTBOUND")
    assert schedule_digests(end) == 1
    assert schedule_digests(end + timedelta(minutes=1)) == 0
    delivery = DigestDelivery.objects.get()
    send_digest(str(delivery.id))
    send_digest(str(delivery.id))
    assert len(mailoutbox) == 1
    email = mailoutbox[0]
    assert email.to == [preference.user.email]
    for text in (
        "Boundary start",
        "Second domain",
        "Message preview",
        str(included.conversation_id),
    ):
        assert text in email.body
    for text in ("Other account secret", "Too old", "Boundary end", "Outgoing"):
        assert text not in email.body
    delivery.refresh_from_db()
    assert delivery.status == DigestDelivery.Status.SENT
    assert delivery.message_count == 2
    assert DurableJob.objects.filter(kind="send_digest").count() == 1


def test_empty_window_skipped(preference, mailoutbox):
    schedule_digests(preference.next_send_at)
    delivery = DigestDelivery.objects.get()
    send_digest(str(delivery.id))
    delivery.refresh_from_db()
    assert delivery.status == DigestDelivery.Status.SKIPPED
    assert not mailoutbox


@pytest.mark.parametrize("change", ["disabled", "inactive", "unverified"])
def test_queued_digest_rechecks_consent_and_user(preference, domain, mailoutbox, change):
    add_message(domain, preference.next_send_at - timedelta(hours=1))
    schedule_digests(preference.next_send_at)
    if change == "disabled":
        preference.enabled = False
        preference.save()
    else:
        user = preference.user
        if change == "inactive":
            user.is_active = False
        else:
            user.email_verified_at = None
        user.save()
    send_digest(str(DigestDelivery.objects.get().id))
    assert not mailoutbox
    assert DigestDelivery.objects.get().status == DigestDelivery.Status.SKIPPED


def test_preview_does_not_expose_quarantine_or_purged_content(preference, domain, mailoutbox):
    add_message(domain, preference.next_send_at - timedelta(hours=1), is_quarantined=True)
    add_message(
        domain, preference.next_send_at - timedelta(hours=1), normalized_purged_at=timezone.now()
    )
    schedule_digests(preference.next_send_at)
    send_digest(str(DigestDelivery.objects.get().id))
    assert "Message preview" not in mailoutbox[0].body
    assert "Quarantined" in mailoutbox[0].body
    assert "expired" in mailoutbox[0].body


def test_retry_uses_same_window(preference, domain, mailoutbox):
    add_message(domain, preference.next_send_at - timedelta(hours=1))
    schedule_digests(preference.next_send_at)
    delivery = DigestDelivery.objects.get()
    with patch("inbox.services.digests.send_mail", side_effect=RuntimeError("Unavailable")):
        assert run_due_jobs(limit=1)["retry"] == 1
    delivery.refresh_from_db()
    assert delivery.status == DigestDelivery.Status.PENDING
    job = DurableJob.objects.get(kind="send_digest")
    job.due_at = timezone.now()
    job.save()
    assert run_due_jobs(limit=1)["complete"] == 1
    assert len(mailoutbox) == 1
    assert DigestDelivery.objects.get().period_end == preference.next_send_at


def test_scheduler_recovers_latest_window(preference):
    now = preference.next_send_at + timedelta(days=3, minutes=15)
    assert schedule_digests(now) == 1
    assert DigestDelivery.objects.get().period_end == now - timedelta(minutes=15)
    preference.refresh_from_db()
    assert preference.next_send_at == now + timedelta(days=1, minutes=-15)


def test_scheduler_integration_and_custom_lookback(preference):
    preference.window_hours = 48
    preference.save()
    schedule_work(preference.next_send_at)
    delivery = DigestDelivery.objects.get()
    assert delivery.period_end - delivery.period_start == timedelta(hours=48)


@pytest.mark.parametrize(
    "zone,clock,after,expected",
    [
        ("Europe/Istanbul", time(9), "2026-10-30T05:00:00+00:00", "2026-10-30T06:00:00+00:00"),
        ("Europe/Istanbul", time(9), "2026-10-30T06:00:00+00:00", "2026-10-31T06:00:00+00:00"),
        ("America/New_York", time(2, 30), "2026-03-08T06:00:00+00:00", "2026-03-08T07:30:00+00:00"),
        ("America/New_York", time(1, 30), "2026-11-01T05:40:00+00:00", "2026-11-02T06:30:00+00:00"),
    ],
)
def test_timezone_and_dst(zone, clock, after, expected):
    preference = DigestSettings(timezone_name=zone, send_time=clock)
    assert next_digest_at(preference, datetime.fromisoformat(after)) == datetime.fromisoformat(
        expected
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("window_hours", 0),
        ("window_hours", 169),
        ("timezone_name", "Invalid/Zone"),
        ("send_time", "25:00"),
    ],
)
def test_invalid_settings(field, value):
    data = {"enabled": True, "window_hours": 24, "timezone_name": "UTC", "send_time": "09:00"}
    data[field] = value
    form = DigestSettingsForm(data)
    assert not form.is_valid()
    assert field in form.errors


def test_settings_save_enable_disable_and_validation(client, owner):
    client.force_login(owner)
    response = client.get(reverse("settings"))
    assert response.status_code == 200
    assert "Digest" in response.content.decode()
    assert not DigestSettings.objects.get(user=owner).enabled
    data = {
        "enabled": "on",
        "window_hours": 48,
        "send_time": "08:15",
        "timezone_name": "Europe/Istanbul",
    }
    assert client.post(reverse("settings"), data).status_code == 302
    preference = DigestSettings.objects.get(user=owner)
    assert preference.enabled and preference.window_hours == 48
    assert preference.next_send_at > timezone.now()
    previous_due = preference.next_send_at
    assert client.post(reverse("settings"), data).status_code == 302
    preference.refresh_from_db()
    assert preference.next_send_at == previous_due
    invalid = {**data, "timezone_name": "invalid"}
    assert client.post(reverse("settings"), invalid).status_code == 200
    preference.refresh_from_db()
    assert preference.timezone_name == "Europe/Istanbul"
    data.pop("enabled")
    client.post(reverse("settings"), data)
    preference.refresh_from_db()
    assert not preference.enabled and preference.next_send_at is None


def test_settings_requires_login(client):
    assert client.post(reverse("settings"), {"enabled": "on"}).status_code == 302
    assert not DigestSettings.objects.exists()


def test_settings_change_cancels_previous_queued_digest(client, preference, domain, mailoutbox):
    add_message(domain, preference.next_send_at - timedelta(hours=1))
    schedule_digests(preference.next_send_at)
    client.force_login(preference.user)
    data = {"window_hours": 24, "send_time": "09:00", "timezone_name": "UTC"}
    client.post(reverse("settings"), data)
    client.post(reverse("settings"), {**data, "enabled": "on"})
    delivery = DigestDelivery.objects.get()
    assert delivery.status == DigestDelivery.Status.SKIPPED
    send_digest(str(delivery.id))
    assert not mailoutbox
