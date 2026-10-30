from urllib.parse import urlsplit

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from inbox.models import OutboundMessage, ReplyDraft
from inbox.services.drafts import revise_draft
from inbox.services.notifications import (
    create_domain_drift_notifications,
    create_outbound_problem_notifications,
    create_security_notifications,
    send_pending_email_notification,
)

pytestmark = pytest.mark.django_db


def assert_email_target(client, owner, mailoutbox, notification, path):
    assert send_pending_email_notification(notification)
    url = f"https://inbox.example{path}"
    assert mailoutbox[-1].body.endswith(f"\n\n{url}")
    client.force_login(owner)
    target = urlsplit(url)
    response = client.get(target.path)
    assert response.status_code == 200
    if target.fragment:
        assert f'id="{target.fragment}"' in response.content.decode()


@pytest.mark.parametrize("quarantined", [False, True])
@override_settings(PUBLIC_BASE_URL="https://inbox.example/")
def test_security_email_links_to_exact_message(
    client, owner, mailoutbox, inbound_message, quarantined
):
    inbound_message.is_suspicious = True
    inbound_message.is_quarantined = quarantined
    inbound_message.save()
    notification = create_security_notifications(inbound_message)[0]
    path = (
        reverse("conversation_detail", args=[inbound_message.conversation_id])
        + f"#message-{inbound_message.id}"
    )
    assert_email_target(client, owner, mailoutbox, notification, path)


@pytest.mark.parametrize("status", ["FAILED", "UNKNOWN", "BOUNCED", "COMPLAINED"])
@override_settings(PUBLIC_BASE_URL="https://inbox.example")
def test_outbound_problem_email_links_to_exact_attempt(
    client, owner, mailoutbox, inbound_message, status
):
    draft = ReplyDraft.objects.create(
        domain=inbound_message.domain,
        conversation=inbound_message.conversation,
        context_message=inbound_message,
    )
    revision = revise_draft(draft=draft, owner=owner, subject="Reply", body_text="Reply text.")
    outbound = OutboundMessage.objects.create(
        domain=inbound_message.domain,
        conversation=inbound_message.conversation,
        revision=revision,
        status=status,
        from_address="owner@example.com",
        to_address="sender@example.net",
        subject=revision.subject,
        body_text=revision.body_text,
        content_hash=revision.content_hash,
        rfc_message_id="<reply@example.com>",
    )
    notification = create_outbound_problem_notifications(outbound)[0]
    path = (
        reverse("conversation_detail", args=[outbound.conversation_id]) + f"#outbound-{outbound.id}"
    )
    assert_email_target(client, owner, mailoutbox, notification, path)


@override_settings(PUBLIC_BASE_URL="https://inbox.example")
def test_dns_drift_email_links_to_domain(client, owner, mailoutbox, domain):
    domain.last_checked_at = timezone.now()
    domain.save()
    notification = create_domain_drift_notifications(domain)[0]
    assert_email_target(
        client,
        owner,
        mailoutbox,
        notification,
        reverse("domain_detail", args=[domain.id]),
    )
