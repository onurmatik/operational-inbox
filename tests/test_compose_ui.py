from __future__ import annotations

import uuid

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from inbox.models import (
    AuditEvent,
    Conversation,
    Domain,
    DurableJob,
    OutboundControl,
    OutboundMessage,
    ReplyDraft,
    User,
)


def _compose_data(compose_domain: Domain, **changes) -> dict[str, str]:
    return {
        "domain": str(compose_domain.id),
        "from_local_part": "onur",
        "to_address": "recipient@example.net",
        "subject": "A new conversation",
        "body_text": "Hello,\n\nThis is a new email.\n",
        "idempotency_key": str(uuid.uuid4()),
        **changes,
    }


def _login_domain(client: Client, owner: User, domain: Domain) -> None:
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(domain.id)
    session.save()


@pytest.mark.django_db
def test_compose_get_uses_selected_domain_without_persisting_a_draft(client, owner, domain):
    Domain.objects.create(
        owner=owner, hostname="another.example", claim_expires_at=domain.claim_expires_at
    )
    _login_domain(client, owner, domain)

    response = client.get(reverse("compose"))
    second = client.get(reverse("compose"))

    assert response.status_code == 200
    assert response.context["form"]["domain"].value() == domain.id
    assert uuid.UUID(response.context["form"]["idempotency_key"].value()).version == 4
    assert (
        response.context["form"]["idempotency_key"].value()
        != second.context["form"]["idempotency_key"].value()
    )
    assert b"@example.com" in response.content
    assert b">Send</button>" in response.content
    assert b">Cancel</a>" in response.content
    assert b"Save draft" not in response.content
    assert not Conversation.objects.exists()
    assert not ReplyDraft.objects.exists()


@pytest.mark.django_db
def test_inbox_and_outbox_compose_links_keep_the_domain(client, owner, domain, inbound_message):
    _login_domain(client, owner, domain)
    link = f"{reverse('compose')}?domain={domain.id}".encode()

    assert link in client.get(reverse("inbox")).content
    assert link in client.get(f"{reverse('outbox')}?domain={domain.id}").content


@pytest.mark.django_db
def test_compose_post_queues_owner_send_and_redirects_to_domain_outbox(client, owner, domain):
    _login_domain(client, owner, domain)
    data = _compose_data(domain)

    response = client.post(reverse("compose"), data)

    assert response.status_code == 302
    assert response.url == f"{reverse('outbox')}?domain={domain.id}"
    outbound = OutboundMessage.objects.get()
    assert outbound.from_address == "onur@example.com"
    assert outbound.to_address == data["to_address"]
    assert outbound.body_text == data["body_text"]
    assert outbound.status == OutboundMessage.Status.QUEUED
    assert outbound.authorization_mode == OutboundMessage.AuthorizationMode.OWNER_APPROVAL
    assert outbound.revision.draft.kind == ReplyDraft.Kind.COMPOSE
    assert outbound.revision.approval.content_hash == outbound.content_hash
    assert DurableJob.objects.filter(kind="send_outbound").count() == 1
    assert AuditEvent.objects.filter(event_type="outbound.composed").count() == 1
    assert any("Queued" in str(message) for message in get_messages(response.wsgi_request))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "error_field"),
    [
        ("domain", "", "domain"),
        ("from_local_part", "", "from_local_part"),
        ("from_local_part", "onur@elsewhere.example", "from_local_part"),
        ("from_local_part", "onur\r\n", "from_local_part"),
        ("to_address", "One <one@example.net>", "to_address"),
        ("to_address", "one@example.net,two@example.net", "to_address"),
        ("to_address", "one@example.net\r\n", "to_address"),
        ("subject", "", "subject"),
        ("subject", "Hi\r\nBcc: extra@example.net", "subject"),
        ("subject", "x" * 999, "subject"),
        ("body_text", " ", "body_text"),
        ("body_text", "x" * 20001, "body_text"),
        ("idempotency_key", "invalid", "idempotency_key"),
    ],
)
def test_compose_errors_preserve_inputs_without_records(
    client, owner, domain, field, value, error_field
):
    _login_domain(client, owner, domain)
    data = _compose_data(domain, **{field: value})

    response = client.post(reverse("compose"), data)

    assert response.status_code == 200
    form = response.context["form"]
    assert error_field in form.errors
    for name, original in data.items():
        assert form[name].value() == original
    assert not Conversation.objects.exists()
    assert not ReplyDraft.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not DurableJob.objects.filter(kind="send_outbound").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("blocked", ["paused", "not_ready", "quota"])
def test_compose_sending_errors_preserve_fields(client, owner, domain, settings, blocked):
    _login_domain(client, owner, domain)
    if blocked == "paused":
        OutboundControl.objects.create(user=owner, is_paused=True)
    elif blocked == "not_ready":
        domain.outbound_ready = False
        domain.save(update_fields=("outbound_ready", "updated_at"))
    else:
        settings.OUTBOUND_MONTHLY_ACCOUNT_LIMIT = 1
        assert client.post(reverse("compose"), _compose_data(domain)).status_code == 302
    data = _compose_data(domain)

    response = client.post(reverse("compose"), data)

    assert response.status_code == 200
    assert response.context["form"].non_field_errors()
    for name, original in data.items():
        assert response.context["form"][name].value() == original
    assert OutboundMessage.objects.count() == (1 if blocked == "quota" else 0)


@pytest.mark.django_db
def test_compose_domain_is_owner_scoped_on_get_and_post(client, owner, domain):
    other = User.objects.create_user(email="other@example.org", password="Another-Password-123")
    foreign_domain = Domain.objects.create(
        owner=other, hostname="private.example", claim_expires_at=domain.claim_expires_at
    )
    _login_domain(client, owner, domain)

    assert client.get(f"{reverse('compose')}?domain={foreign_domain.id}").status_code == 404
    assert client.get(f"{reverse('compose')}?domain=not-a-uuid").status_code == 404
    response = client.post(reverse("compose"), _compose_data(foreign_domain))

    assert response.status_code == 200
    assert "domain" in response.context["form"].errors
    assert b"private.example" not in response.content
    assert not OutboundMessage.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("status", [OutboundMessage.Status.FAILED, OutboundMessage.Status.UNKNOWN])
def test_compose_replay_keeps_failed_or_unknown_attempt(client, owner, domain, status):
    _login_domain(client, owner, domain)
    data = _compose_data(domain)
    assert client.post(reverse("compose"), data).status_code == 302
    outbound = OutboundMessage.objects.get()
    outbound.status = status
    outbound.save(update_fields=("status", "updated_at"))

    response = client.post(reverse("compose"), data)

    assert response.status_code == 302
    assert OutboundMessage.objects.count() == 1
    assert DurableJob.objects.filter(kind="send_outbound").count() == 1
    assert any(
        outbound.get_status_display() in str(message)
        for message in get_messages(response.wsgi_request)
    )


@pytest.mark.django_db
def test_compose_idempotency_conflict_retains_edited_content(client, owner, domain):
    _login_domain(client, owner, domain)
    data = _compose_data(domain)
    assert client.post(reverse("compose"), data).status_code == 302
    data["subject"] = "Changed after the send"

    response = client.post(reverse("compose"), data)

    assert response.status_code == 409
    assert response.context["form"]["subject"].value() == data["subject"]
    assert response.context["form"]["idempotency_key"].value() == data["idempotency_key"]
    assert response.context["form"].non_field_errors()
    assert OutboundMessage.objects.count() == 1


@pytest.mark.django_db
def test_compose_snapshot_is_hidden_from_reply_actions_and_empty_inbox(client, owner, domain):
    _login_domain(client, owner, domain)
    assert client.post(reverse("compose"), _compose_data(domain)).status_code == 302
    outbound = OutboundMessage.objects.get()
    snapshot = outbound.revision.draft

    inbox = client.get(reverse("inbox"))
    detail = client.get(reverse("conversation_detail", args=[outbound.conversation_id]))

    assert list(inbox.context["conversations"]) == []
    assert detail.context["draft"] is None
    assert client.post(reverse("draft_revise", args=[snapshot.id])).status_code == 404
    assert client.post(reverse("draft_approve", args=[snapshot.id])).status_code == 404


@pytest.mark.django_db
def test_compose_requires_authenticated_verified_owner_and_csrf(client, owner, domain):
    url = reverse("compose")
    assert client.get(url).status_code == 302
    _login_domain(client, owner, domain)
    protected_client = Client(enforce_csrf_checks=True)
    protected_client.force_login(owner)
    assert protected_client.post(url, _compose_data(domain)).status_code == 403
    owner.email_verified_at = None
    owner.save(update_fields=("email_verified_at",))
    assert client.get(url).url == reverse("verification_sent")


@pytest.mark.django_db
def test_compose_without_domain_redirects_to_setup(client, owner):
    client.force_login(owner)
    assert client.get(reverse("compose")).url == reverse("domain_create")
