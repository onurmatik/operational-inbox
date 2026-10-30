from __future__ import annotations

import pytest
from django.db import transaction
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.models import AuditEvent, Domain, DurableJob, Message, Notification, User
from inbox.services.jobs import schedule_work
from inbox.services.notifications import (
    create_security_notifications,
    send_pending_email_notification,
)
from inbox.services.spam_review import apply_spam_review

pytestmark = pytest.mark.django_db


@pytest.fixture
def flagged(inbound_message):
    inbound_message.spam_verdict = Message.Verdict.FAIL
    inbound_message.is_suspicious = True
    inbound_message.save()
    return inbound_message


def endpoint(message):
    return f"/api/v1/domains/{message.domain_id}/messages/{message.id}/spam-review"


def review(client, message, action="not_spam"):
    return client.post(endpoint(message), {"action": action}, content_type="application/json")


def test_review_undo_preserves_verdict_filters_and_audit(client, owner, flagged):
    client.force_login(owner)
    original = (
        flagged.spam_verdict,
        flagged.spf_verdict,
        flagged.dkim_verdict,
        flagged.dmarc_verdict,
        flagged.virus_verdict,
    )
    result = review(client, flagged)
    assert result.status_code == 200
    assert result.json()["changed"] is True
    assert result.json()["security"]["spam"] == "FAIL"
    assert result.json()["not_spam_at"]
    assert result.json()["is_suspicious"] is False
    assert client.get("/api/v1/feed/messages", {"security": "suspicious"}).json()["items"] == []
    feed = client.get("/api/v1/feed/messages").json()["items"][0]
    assert feed["not_spam_at"] == result.json()["not_spam_at"]
    detail = client.get(
        f"/api/v1/domains/{flagged.domain_id}/conversations/{flagged.conversation_id}"
    ).json()
    assert detail["messages"][0]["not_spam_at"]
    assert review(client, flagged).json()["changed"] is False
    event = AuditEvent.objects.get(event_type="message.marked_not_spam")
    assert event.actor_id == owner.id
    assert event.object_id == flagged.id
    assert event.metadata["spam_verdict"] == "FAIL"
    assert event.metadata["not_spam_at"]
    assert review(client, flagged, "undo").json()["is_suspicious"] is True
    assert review(client, flagged, "undo").json()["changed"] is False
    flagged.refresh_from_db()
    assert flagged.not_spam_at is None
    assert original == (
        flagged.spam_verdict,
        flagged.spf_verdict,
        flagged.dkim_verdict,
        flagged.dmarc_verdict,
        flagged.virus_verdict,
    )
    assert AuditEvent.objects.filter(event_type__startswith="message.").count() == 2
    assert len(client.get("/api/v1/feed/messages", {"security": "suspicious"}).json()["items"]) == 1


@pytest.mark.parametrize("field", ["spf_verdict", "dkim_verdict", "dmarc_verdict"])
def test_authentication_failure_and_alert_remain(client, owner, flagged, field):
    setattr(flagged, field, Message.Verdict.FAIL)
    flagged.save()
    notification = create_security_notifications(flagged)[0]
    client.force_login(owner)
    result = review(client, flagged)
    assert result.status_code == 200
    assert result.json()["is_suspicious"] is True
    notification.refresh_from_db()
    assert notification.status == Notification.Status.PENDING
    page = client.get(
        reverse("conversation_detail", args=[flagged.conversation_id])
    ).content.decode()
    assert "Sender authentication failed." in page
    assert "Marked as not spam" in page
    assert ">Undo</button>" in page


@pytest.mark.parametrize(
    "field,value",
    [
        ("is_quarantined", True),
        ("virus_verdict", "UNKNOWN"),
        ("direction", "OUTBOUND"),
        ("spam_verdict", "PASS"),
    ],
)
def test_invalid_targets_cannot_be_overridden(client, owner, flagged, field, value):
    setattr(flagged, field, value)
    flagged.save()
    client.force_login(owner)
    assert review(client, flagged).status_code == 409
    flagged.refresh_from_db()
    assert flagged.not_spam_at is None
    assert not AuditEvent.objects.filter(event_type="message.marked_not_spam").exists()


@pytest.mark.parametrize("verdict", ["GRAY", "UNKNOWN"])
def test_non_pass_spam_can_be_reviewed(client, owner, flagged, verdict):
    flagged.spam_verdict = verdict
    flagged.save()
    client.force_login(owner)
    result = review(client, flagged).json()
    assert result["security"]["spam"] == verdict
    assert result["is_suspicious"] is False


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
@pytest.mark.parametrize("initial_status", ["PENDING", "FAILED"])
def test_resolved_alert_cannot_send_from_stale_job_and_undo_requeues(
    client, owner, flagged, mailoutbox, initial_status
):
    notification = create_security_notifications(flagged)[0]
    notification.status = initial_status
    notification.save()
    job = DurableJob.objects.create(
        domain=flagged.domain,
        kind="send_notification",
        idempotency_key=f"notification:{notification.id}",
        payload={"notification_id": str(notification.id)},
        due_at=timezone.now(),
        status=DurableJob.Status.LEASED,
    )
    client.force_login(owner)
    assert review(client, flagged).status_code == 200
    # A worker may have fetched its Notification before the review.
    assert send_pending_email_notification(notification) is True
    assert len(mailoutbox) == 0
    notification.refresh_from_db()
    assert notification.status == Notification.Status.RESOLVED
    assert review(client, flagged, "undo").status_code == 200
    # Worker completes its earlier no-op after Undo; scheduling still recovers the alert.
    job.status = DurableJob.Status.COMPLETE
    job.save()
    schedule_work()
    job.refresh_from_db()
    assert job.status == DurableJob.Status.PENDING
    assert send_pending_email_notification(notification) is True
    assert len(mailoutbox) == 1
    assert send_pending_email_notification(notification) is True
    assert len(mailoutbox) == 1


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_sent_and_unrelated_notifications_remain(client, owner, flagged, mailoutbox):
    notification = create_security_notifications(flagged)[0]
    assert send_pending_email_notification(notification)
    unrelated = Notification.objects.create(
        domain=flagged.domain,
        kind="domain_drift",
        dedupe_key="unrelated",
        channel=Notification.Channel.EMAIL,
        title="DNS",
        body="DNS drift",
    )
    client.force_login(owner)
    review(client, flagged)
    review(client, flagged, "undo")
    assert send_pending_email_notification(notification)
    assert len(mailoutbox) == 1
    notification.refresh_from_db()
    unrelated.refresh_from_db()
    assert notification.status == Notification.Status.SENT
    assert unrelated.status == Notification.Status.PENDING


def test_audit_failure_rolls_back_review_and_notification(client, owner, flagged, monkeypatch):
    notification = create_security_notifications(flagged)[0]
    client.force_login(owner)

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("inbox.api.record_api_audit", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        review(client, flagged)
    flagged.refresh_from_db()
    notification.refresh_from_db()
    assert flagged.not_spam_at is None
    assert flagged.is_suspicious
    assert notification.status == Notification.Status.PENDING


def test_ui_review_undo_and_csrf(client, owner, flagged):
    client.force_login(owner)
    page_url = reverse("conversation_detail", args=[flagged.conversation_id])
    url = reverse("message_spam_review", args=[flagged.id])
    page = client.get(page_url).content.decode()
    assert ">Not spam</button>" in page
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(owner)
    assert csrf_client.post(url, {"action": "not_spam"}).status_code == 403
    assert client.get(url).status_code == 405
    response = client.post(url, {"action": "not_spam"}, follow=True)
    assert response.status_code == 200
    page = response.content.decode()
    assert "Marked as not spam" in page and ">Undo</button>" in page
    assert ">Not spam</button>" not in page
    assert "Spam FAIL" in page
    assert "Flagged as possible spam." not in page
    response = client.post(url, {"action": "undo", "next": "https://evil.example/"})
    assert response.status_code == 302 and "evil.example" not in response.url
    assert ">Not spam</button>" in client.get(page_url).content.decode()
    assert (
        AuditEvent.objects.filter(actor_id=owner.id, event_type__startswith="message.").count() == 2
    )
    assert client.post(url, {"action": "invalid"}).status_code == 409


def test_tenant_and_disabled_domain_boundaries(client, owner, flagged):
    other = User.objects.create_user(email="other@example.org", email_verified_at=timezone.now())
    client.force_login(other)
    assert review(client, flagged).status_code == 404
    assert (
        client.post(
            reverse("message_spam_review", args=[flagged.id]), {"action": "not_spam"}
        ).status_code
        == 404
    )
    client.force_login(owner)
    flagged.domain.status = Domain.Status.DISABLED
    flagged.domain.save()
    assert review(client, flagged).status_code == 404
    assert (
        client.post(
            reverse("message_spam_review", args=[flagged.id]), {"action": "not_spam"}
        ).status_code
        == 404
    )


def test_read_only_domain_cannot_review(client, owner, flagged, monkeypatch):
    client.force_login(owner)
    monkeypatch.setattr("inbox.api.can_manage_domain", lambda *_: False)
    monkeypatch.setattr("inbox.views.can_manage_domain", lambda *_: False)
    assert review(client, flagged).status_code == 403
    client.post(reverse("message_spam_review", args=[flagged.id]), {"action": "not_spam"})
    flagged.refresh_from_db()
    assert flagged.not_spam_at is None


def test_quarantine_never_resolves_security_alert(client, owner, flagged):
    notification = create_security_notifications(flagged)[0]
    flagged.is_quarantined = True
    flagged.save()
    client.force_login(owner)
    assert review(client, flagged).status_code == 409
    notification.refresh_from_db()
    assert notification.status == Notification.Status.PENDING
    page = client.get(
        reverse("conversation_detail", args=[flagged.conversation_id])
    ).content.decode()
    assert ">Not spam</button>" not in page
    assert "Quarantined content is locked." in page


def test_service_requires_supported_action(flagged):
    from django.core.exceptions import ValidationError

    with transaction.atomic(), pytest.raises(ValidationError):
        message = Message.objects.select_for_update().get(id=flagged.id)
        apply_spam_review(message, "invalid")
