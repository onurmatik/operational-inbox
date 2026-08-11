from __future__ import annotations

import importlib
from datetime import timedelta
from html.parser import HTMLParser

import pytest
from django.apps import apps
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from inbox.models import (
    AuditEvent,
    BillingProfile,
    Conversation,
    ConversationTag,
    Domain,
    Message,
    MessageRecipient,
    User,
)


def _select_domain(client: Client, owner: User, domain: Domain) -> None:
    client.force_login(owner)
    session = client.session
    session["domain_id"] = str(domain.id)
    session.save()


def _message(
    conversation: Conversation,
    provider_message_id: str,
    *,
    direction: str = Message.Direction.INBOUND,
    viewed_at=None,
    received_at=None,
) -> Message:
    received_at = received_at or timezone.now()
    return Message.objects.create(
        domain=conversation.domain,
        conversation=conversation,
        direction=direction,
        provider_message_id=provider_message_id,
        rfc_message_id=f"<{provider_message_id}@example.net>",
        from_address="sender@example.net",
        subject=conversation.subject,
        text_body=f"Body for {provider_message_id}",
        received_at=received_at,
        viewed_at=viewed_at,
    )


def _routing_recipient(message: Message, address: str) -> MessageRecipient:
    return MessageRecipient.objects.create(
        domain=message.domain,
        message=message,
        kind=MessageRecipient.Kind.ENVELOPE,
        address=address,
        is_routing_recipient=True,
    )


class _ActionNestingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchor_depth = 0
        self.form_inside_anchor = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchor_depth += 1
        elif tag == "form" and self.anchor_depth:
            self.form_inside_anchor = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.anchor_depth -= 1


@pytest.mark.django_db
def test_message_tracking_migration_backfills_only_existing_inbound_and_adds_indexes(
    conversation,
    inbound_message,
):
    outbound = _message(
        conversation,
        "historical-outbound",
        direction=Message.Direction.OUTBOUND,
    )
    migration = importlib.import_module("inbox.migrations.0008_message_tracking")

    migration.mark_existing_inbound_messages_viewed(apps, None)

    inbound_message.refresh_from_db()
    outbound.refresh_from_db()
    assert inbound_message.viewed_at == inbound_message.received_at
    assert outbound.viewed_at is None
    assert outbound.is_new is False
    new_inbound = _message(conversation, "new-after-migration")
    assert new_inbound.viewed_at is None
    assert new_inbound.is_new is True

    with connection.cursor() as cursor:
        conversation_indexes = connection.introspection.get_constraints(
            cursor, Conversation._meta.db_table
        )
        message_indexes = connection.introspection.get_constraints(cursor, Message._meta.db_table)
    assert conversation_indexes["inbox_conve_domain__3932b5_idx"]["index"] is True
    assert message_indexes["inbox_messa_domain__8dfcf6_idx"]["index"] is True


@pytest.mark.django_db
def test_detail_marks_inbound_messages_viewed_once_and_writes_one_audit(
    client,
    owner,
    domain,
    conversation,
    inbound_message,
):
    _select_domain(client, owner, domain)
    outbound = _message(
        conversation,
        "detail-outbound",
        direction=Message.Direction.OUTBOUND,
    )
    url = reverse("conversation_detail", args=[conversation.id])

    assert client.get(url).status_code == 200
    first_viewed_at = Message.objects.get(id=inbound_message.id).viewed_at
    assert first_viewed_at is not None
    assert Message.objects.get(id=outbound.id).viewed_at is None
    events = AuditEvent.objects.filter(
        domain=domain,
        event_type="conversation.viewed",
        object_id=conversation.id,
    )
    assert events.count() == 1
    assert events.get().metadata == {"messages_viewed": 1}

    assert client.get(url).status_code == 200
    inbound_message.refresh_from_db()
    assert inbound_message.viewed_at == first_viewed_at
    assert events.count() == 1


@pytest.mark.django_db
def test_quick_actions_are_idempotent_restoreable_and_preserve_return_location(
    client,
    owner,
    domain,
    conversation,
    inbound_message,
):
    _select_domain(client, owner, domain)
    action_url = reverse("conversation_action", args=[conversation.id])
    next_url = f"{reverse('inbox')}?domain={domain.id}&folder=starred&page=2"

    for action in ("star", "star", "archive", "archive"):
        response = client.post(action_url, {"action": action, "next": next_url})
        assert response.status_code == 302
        assert response.url == next_url

    conversation.refresh_from_db()
    inbound_message.refresh_from_db()
    assert conversation.starred_at is not None
    assert conversation.archived_at is not None
    assert conversation.trashed_at is None
    assert inbound_message.viewed_at is None
    assert AuditEvent.objects.filter(event_type="conversation.starred").count() == 1
    assert AuditEvent.objects.filter(event_type="conversation.archived").count() == 1

    archived_page = client.get(
        reverse("inbox"),
        {"domain": domain.id, "folder": "archive"},
    )
    assert archived_page.status_code == 200
    assert b'aria-label="Unstar conversation"' in archived_page.content
    assert b'data-icon="star-filled"' in archived_page.content
    assert b'data-tooltip="Restore to Inbox"' in archived_page.content
    assert b'data-icon="restore"' in archived_page.content

    for action in ("trash", "trash", "restore", "restore", "unstar", "unstar"):
        client.post(action_url, {"action": action, "next": next_url})
    conversation.refresh_from_db()
    assert conversation.archived_at is None
    assert conversation.trashed_at is None
    assert conversation.starred_at is None
    assert AuditEvent.objects.filter(event_type="conversation.trashed").count() == 1
    assert AuditEvent.objects.filter(event_type="conversation.restored").count() == 1
    assert AuditEvent.objects.filter(event_type="conversation.unstarred").count() == 1

    inbox_page = client.get(reverse("inbox"), {"domain": domain.id})
    assert inbox_page.status_code == 200
    assert b'data-icon="start"' not in inbox_page.content
    assert b'data-icon="started"' not in inbox_page.content


@pytest.mark.django_db
def test_conversation_tags_are_free_form_idempotent_and_filterable(
    client,
    owner,
    domain,
    conversation,
):
    _select_domain(client, owner, domain)
    url = reverse("conversation_tag", args=[conversation.id])
    detail_url = reverse("conversation_detail", args=[conversation.id])

    for value in (" Customer Request ", "#customer   request"):
        response = client.post(
            url,
            {"operation": "add", "tag": value, "next": detail_url},
        )
        assert response.status_code == 302
        assert response.url == detail_url

    tag = ConversationTag.objects.get(conversation=conversation)
    assert tag.name == "Customer Request"
    assert tag.normalized_name == "customer request"
    assert AuditEvent.objects.filter(event_type="conversation.tag_added").count() == 1

    filtered = client.get(reverse("inbox"), {"domain": domain.id, "tag": "CUSTOMER REQUEST"})
    assert filtered.status_code == 200
    assert list(filtered.context["conversations"]) == [conversation]

    for value in (tag.name, tag.name):
        client.post(url, {"operation": "remove", "tag": value, "next": detail_url})
    assert not ConversationTag.objects.filter(conversation=conversation).exists()
    assert AuditEvent.objects.filter(event_type="conversation.tag_removed").count() == 1


@pytest.mark.django_db
def test_sidebar_counts_unique_new_messages_per_domain_and_per_routing_address(
    client,
    owner,
    domain,
    conversation,
    inbound_message,
):
    now = timezone.now()
    second_conversation = Conversation.objects.create(
        domain=domain,
        subject="Second request",
        normalized_subject="second request",
        first_message_at=now,
        last_message_at=now,
        last_inbound_at=now,
    )
    second_inbound = _message(second_conversation, "second-inbound")
    outbound = _message(
        conversation,
        "counts-outbound",
        direction=Message.Direction.OUTBOUND,
    )
    _routing_recipient(inbound_message, "support@example.com")
    _routing_recipient(inbound_message, "contact@example.com")
    _routing_recipient(second_inbound, "support@example.com")
    _routing_recipient(outbound, "ignored@example.com")
    _select_domain(client, owner, domain)

    response = client.get(reverse("inbox"), {"domain": domain.id})

    assert response.status_code == 200
    tree_domain = next(
        item for item in response.context["nav_inbox_tree"] if item["id"] == domain.id
    )
    address_counts = {item["address"]: item["new_count"] for item in tree_domain["addresses"]}
    assert tree_domain["new_count"] == 2
    assert address_counts == {"contact@example.com": 1, "support@example.com": 2}
    assert b"ignored@example.com" not in response.content

    filtered = client.get(
        reverse("inbox"),
        {"domain": domain.id, "recipient": "contact@example.com"},
    )
    assert list(filtered.context["conversations"]) == [conversation]
    assert b"contact@example.com" in filtered.content

    conversation.archived_at = timezone.now()
    conversation.save(update_fields=("archived_at", "updated_at"))
    second_conversation.trashed_at = timezone.now()
    second_conversation.save(update_fields=("trashed_at", "updated_at"))
    emptied = client.get(reverse("inbox"), {"domain": domain.id})
    tree_domain = next(
        item for item in emptied.context["nav_inbox_tree"] if item["id"] == domain.id
    )
    assert tree_domain["new_count"] == 0
    assert all(item["new_count"] == 0 for item in tree_domain["addresses"])


@pytest.mark.django_db
def test_inbox_rows_keep_detail_link_separate_from_post_actions(
    client,
    owner,
    domain,
    conversation,
    inbound_message,
):
    _routing_recipient(inbound_message, "support@example.com")
    _select_domain(client, owner, domain)

    response = client.get(reverse("inbox"), {"domain": domain.id})

    parser = _ActionNestingParser()
    parser.feed(response.content.decode())
    assert parser.form_inside_anchor is False
    assert b"1 new" in response.content
    assert b"support@example.com" in response.content
    assert reverse("conversation_action", args=[conversation.id]).encode() in response.content
    assert b'aria-label="Star conversation"' in response.content
    assert b'aria-label="Archive conversation"' in response.content
    assert b'data-tooltip="Move to Trash"' in response.content
    assert b'data-icon="star"' in response.content
    assert b'data-icon="start"' not in response.content
    assert b'data-icon="archive"' in response.content
    assert b'data-icon="trash"' in response.content
    assert b"iconify-icon" not in response.content


@pytest.mark.django_db
def test_cross_owner_filters_and_actions_are_not_found(
    client,
    owner,
    domain,
    conversation,
):
    other_owner = User.objects.create_user(
        email="other-owner@example.com",
        password="Correct-Horse-Battery-456",
        email_verified_at=timezone.now(),
    )
    other_domain = Domain.objects.create(
        owner=other_owner,
        hostname="other.example",
        status=Domain.Status.READY,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    now = timezone.now()
    other_conversation = Conversation.objects.create(
        domain=other_domain,
        subject="Other owner's message",
        normalized_subject="other owner's message",
        first_message_at=now,
        last_message_at=now,
    )
    other_message = _message(other_conversation, "other-owner-inbound")
    _routing_recipient(other_message, "private@other.example")
    _select_domain(client, owner, domain)

    assert client.get(reverse("inbox"), {"domain": other_domain.id}).status_code == 404
    assert client.get(reverse("inbox"), {"recipient": "private@other.example"}).status_code == 404
    action_url = reverse("conversation_action", args=[other_conversation.id])
    assert client.post(action_url, {"action": "star"}).status_code == 404
    assert client.get(reverse("conversation_action", args=[conversation.id])).status_code == 405


@pytest.mark.django_db
def test_quick_actions_require_csrf(client, owner, domain, conversation):
    protected_client = Client(enforce_csrf_checks=True)
    _select_domain(protected_client, owner, domain)
    action_url = reverse("conversation_action", args=[conversation.id])

    inbox = protected_client.get(reverse("inbox"), {"domain": domain.id})
    assert inbox.status_code == 200
    assert protected_client.post(action_url, {"action": "star"}).status_code == 403

    csrf_token = protected_client.cookies["csrftoken"].value
    allowed = protected_client.post(
        action_url,
        {"action": "star", "csrfmiddlewaretoken": csrf_token},
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert allowed.status_code == 302


@pytest.mark.django_db
def test_viewed_transition_works_on_read_only_domain_but_manual_actions_require_selection(
    client,
    owner,
    domain,
):
    profile = BillingProfile.objects.get(user=owner)
    profile.subscription_status = BillingProfile.SubscriptionStatus.NONE
    profile.subscription_plan = ""
    profile.save(update_fields=("subscription_status", "subscription_plan", "updated_at"))
    read_only_domain = Domain.objects.create(
        owner=owner,
        hostname="read-only.example",
        status=Domain.Status.READY,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    now = timezone.now()
    read_only_conversation = Conversation.objects.create(
        domain=read_only_domain,
        subject="Read-only tracking",
        normalized_subject="read-only tracking",
        first_message_at=now,
        last_message_at=now,
        last_inbound_at=now,
    )
    inbound = _message(read_only_conversation, "read-only-inbound")
    _select_domain(client, owner, read_only_domain)

    detail = client.get(reverse("conversation_detail", args=[read_only_conversation.id]))
    inbound.refresh_from_db()
    assert detail.status_code == 200
    assert inbound.viewed_at is not None

    action = client.post(
        reverse("conversation_action", args=[read_only_conversation.id]),
        {"action": "star"},
    )
    read_only_conversation.refresh_from_db()
    assert action.status_code == 302
    assert action.url == reverse("domains")
    assert read_only_conversation.starred_at is None
