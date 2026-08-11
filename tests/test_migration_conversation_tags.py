from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_workflow_state_migrates_to_folders_and_usage_derived_tags(request):
    before = [("inbox", "0008_message_tracking")]
    after = [("inbox", "0009_conversation_tags")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("inbox")
    request.addfinalizer(lambda: MigrationExecutor(connection).migrate(latest))
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    User = old_apps.get_model("inbox", "User")
    Domain = old_apps.get_model("inbox", "Domain")
    Conversation = old_apps.get_model("inbox", "Conversation")

    owner = User.objects.create(email="migration@example.com", password="unused")
    domain = Domain.objects.create(
        owner_id=owner.id,
        hostname="migration.example",
        setup_mode="DIRECT_MX",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    now = timezone.now()
    waiting = Conversation.objects.create(
        domain_id=domain.id,
        subject="Waiting",
        first_message_at=now,
        last_message_at=now,
        status="WAITING_EXTERNAL",
    )
    started = Conversation.objects.create(
        domain_id=domain.id,
        subject="Started",
        first_message_at=now,
        last_message_at=now,
        status="OPEN",
        work_started_at=now,
    )
    resolved = Conversation.objects.create(
        domain_id=domain.id,
        subject="Resolved",
        first_message_at=now,
        last_message_at=now,
        status="RESOLVED",
        resolved_at=now,
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    NewConversation = new_apps.get_model("inbox", "Conversation")
    ConversationTag = new_apps.get_model("inbox", "ConversationTag")
    APIToken = new_apps.get_model("inbox", "APIToken")

    assert ConversationTag.objects.filter(
        conversation_id=waiting.id,
        normalized_name="waiting-external",
    ).exists()
    assert ConversationTag.objects.filter(
        conversation_id=started.id,
        normalized_name="in-progress",
    ).exists()
    assert NewConversation.objects.get(id=resolved.id).archived_at == now
    assert {field.name for field in NewConversation._meta.fields}.isdisjoint(
        {"status", "resolved_at", "work_started_at"}
    )
    assert APIToken._meta.get_field("domain").null is True

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor,
            ConversationTag._meta.db_table,
        )
    assert constraints["uniq_conversation_tag_name"]["unique"] is True
    assert constraints["inbox_tag_domain_name_idx"]["index"] is True
