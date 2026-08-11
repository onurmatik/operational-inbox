from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


@pytest.mark.django_db(transaction=True)
def test_api_tokens_become_global_and_only_newest_active_token_survives(request):
    before = [("inbox", "0010_agent_delegated_outbound")]
    after = [("inbox", "0011_global_api_token")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes("inbox")
    request.addfinalizer(lambda: MigrationExecutor(connection).migrate(latest))
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    User = old_apps.get_model("inbox", "User")
    Domain = old_apps.get_model("inbox", "Domain")
    APIToken = old_apps.get_model("inbox", "APIToken")

    owner = User.objects.create(email="token-migration@example.com", password="unused")
    domain = Domain.objects.create(
        owner_id=owner.id,
        hostname="token-migration.example",
        setup_mode="DIRECT_MX",
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    older = APIToken.objects.create(
        owner_id=owner.id,
        domain_id=domain.id,
        name="Scoped token",
        prefix="oi_older",
        token_hash="a" * 64,
        scopes=["read"],
    )
    newer = APIToken.objects.create(
        owner_id=owner.id,
        domain_id=None,
        name="Global token",
        prefix="oi_newer",
        token_hash="b" * 64,
        scopes=["read", "write"],
    )
    APIToken.objects.filter(id=older.id).update(created_at=timezone.now() - timedelta(days=1))

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    NewAPIToken = new_apps.get_model("inbox", "APIToken")

    field_names = {field.name for field in NewAPIToken._meta.fields}
    assert field_names.isdisjoint({"domain", "name", "scopes"})
    assert NewAPIToken.objects.get(id=older.id).revoked_at is not None
    assert NewAPIToken.objects.get(id=newer.id).revoked_at is None
    assert NewAPIToken.objects.filter(owner_id=owner.id, revoked_at__isnull=True).count() == 1

    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor,
            NewAPIToken._meta.db_table,
        )
    assert constraints["uniq_active_api_token_per_owner"]["unique"] is True
