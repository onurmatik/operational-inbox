from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from inbox.models import AuditEvent, Domain, Notification, Report, User


def _create_items(resource: str, organization: Domain, count: int) -> list[str]:
    ids: list[str] = []
    with freeze_time("2026-08-01 10:00:00"):
        for index in range(count):
            item: Report | Notification | AuditEvent
            if resource == "reports":
                item = Report.objects.create(
                    domain=organization,
                    kind=Report.Kind.HOURLY,
                    schedule_key=f"pagination-{organization.id}-{index}",
                    period_start=timezone.now() - timedelta(hours=1),
                    period_end=timezone.now(),
                    status=Report.Status.READY,
                    title=f"Report {index}",
                )
            elif resource == "notifications":
                item = Notification.objects.create(
                    domain=organization,
                    channel=Notification.Channel.IN_APP,
                    kind="pagination",
                    dedupe_key=f"pagination-{organization.id}-{index}",
                    title=f"Notification {index}",
                )
            else:
                item = AuditEvent.objects.create(
                    domain=organization,
                    actor_type=AuditEvent.ActorType.SYSTEM,
                    event_type=f"pagination.{index}",
                    object_type="PaginationTest",
                    request_id=f"pagination-{index}",
                )
            ids.append(str(item.id))
    return ids


@pytest.mark.django_db
@pytest.mark.parametrize("resource", ["reports", "notifications", "audit"])
def test_collection_cursor_is_stable_and_tenant_scoped(
    client,
    owner: User,
    organization: Domain,
    resource: str,
):
    other_owner = User.objects.create_user(
        email=f"other-{resource}@example.com",
        password="Correct-Horse-Battery-456",
        email_verified_at=timezone.now(),
    )
    other_organization = Domain.objects.create(
        owner=other_owner,
        hostname=f"other-{resource}.example",
        setup_mode=Domain.SetupMode.DIRECT_MX,
        status=Domain.Status.READY,
        claim_expires_at=timezone.now() + timedelta(days=1),
    )
    own_ids = _create_items(resource, organization, 5)
    other_ids = _create_items(resource, other_organization, 2)
    expected_ids = sorted(own_ids, reverse=True)

    client.force_login(owner)
    base = f"/api/v1/domains/{organization.id}/{resource}"
    received_ids: list[str] = []
    cursor = None
    for page_number in range(3):
        response = client.get(base, {"limit": 2, "cursor": cursor} if cursor else {"limit": 2})
        assert response.status_code == 200
        payload = response.json()
        received_ids.extend(item["id"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if page_number < 2:
            assert cursor and "{" not in cursor
        else:
            assert cursor is None

    assert received_ids == expected_ids
    assert not set(received_ids) & set(other_ids)
    assert client.get(f"/api/v1/domains/{other_organization.id}/{resource}").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("resource", ["reports", "notifications", "audit"])
def test_collection_invalid_cursor_uses_api_error_contract(
    client,
    owner: User,
    organization: Domain,
    resource: str,
):
    client.force_login(owner)
    response = client.get(
        f"/api/v1/domains/{organization.id}/{resource}",
        {"cursor": "not-a-valid-cursor"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"
    assert set(response.json()) == {"code", "message", "fields", "request_id"}
    assert response.json()["fields"] == {}
    assert response.json()["request_id"]


@pytest.mark.django_db
def test_collection_cursor_is_bound_to_its_resource(client, owner, organization):
    client.force_login(owner)
    _create_items("reports", organization, 2)
    _create_items("notifications", organization, 2)
    base = f"/api/v1/domains/{organization.id}"
    report_cursor = client.get(f"{base}/reports", {"limit": 1}).json()["next_cursor"]

    response = client.get(f"{base}/notifications", {"cursor": report_cursor})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


@pytest.mark.django_db
def test_collection_limit_is_bounded(client, owner, organization):
    client.force_login(owner)
    _create_items("reports", organization, 101)
    base = f"/api/v1/domains/{organization.id}/reports"

    maximum = client.get(base, {"limit": 1000})
    minimum = client.get(base, {"limit": 0})

    assert maximum.status_code == 200
    assert len(maximum.json()["items"]) == 100
    assert maximum.json()["next_cursor"]
    assert minimum.status_code == 200
    assert len(minimum.json()["items"]) == 1
    assert minimum.json()["next_cursor"]
