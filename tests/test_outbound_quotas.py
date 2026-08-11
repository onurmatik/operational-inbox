from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.utils import timezone

from inbox.models import OutboundMessage, ReplyDraft, ReplyDraftRevision, User
from inbox.services.entitlements import for_user
from inbox.services.outbound import (
    outbound_usage,
    require_outbound_capacity,
    set_outbound_paused,
)


def _create_attempts(
    *,
    owner: User,
    project,
    conversation,
    inbound_message,
    created_at: list[datetime],
) -> list[OutboundMessage]:
    draft = ReplyDraft.objects.create(
        domain=project,
        conversation=conversation,
        context_message=inbound_message,
    )
    revision = ReplyDraftRevision.objects.create(
        domain=project,
        draft=draft,
        number=1,
        subject="Re: Quota test",
        body_text="Quota test reply.",
        author=owner,
    )
    draft.current_revision = revision
    draft.save(update_fields=("current_revision", "updated_at"))

    attempts = []
    parent = None
    for attempt_number, timestamp in enumerate(created_at, start=1):
        attempt = OutboundMessage.objects.create(
            domain=project,
            conversation=conversation,
            revision=revision,
            parent=parent,
            attempt_number=attempt_number,
            authorization_mode=OutboundMessage.AuthorizationMode.DELEGATED_SCOPE,
            status=OutboundMessage.Status.FAILED,
            from_address=f"reply@{project.hostname}",
            to_address="sender@example.net",
            subject=revision.subject,
            body_text=revision.body_text,
            content_hash=revision.content_hash,
            rfc_message_id=f"<quota-{attempt_number}@operationalinbox.com>",
        )
        OutboundMessage.objects.filter(id=attempt.id).update(created_at=timestamp)
        attempt.created_at = timestamp
        attempts.append(attempt)
        parent = attempt
    return attempts


@pytest.mark.django_db
@override_settings(
    FREE_OUTBOUND_RATE_LIMIT_PER_MINUTE=2,
    FREE_OUTBOUND_DAILY_ACCOUNT_LIMIT=10,
    FREE_OUTBOUND_DAILY_DOMAIN_LIMIT=10,
    FREE_OUTBOUND_MONTHLY_ACCOUNT_LIMIT=30,
    OUTBOUND_RATE_LIMIT_PER_MINUTE=30,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=500,
    OUTBOUND_DAILY_DOMAIN_LIMIT=200,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=5000,
)
def test_free_and_pro_share_outbound_features_with_plan_specific_capacity(owner):
    free_owner = User.objects.create_user(
        email="free-outbound@example.com",
        password="Correct-Horse-Battery-456",
        email_verified_at=timezone.now(),
        is_active=True,
    )
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)

    free_entitlements = for_user(free_owner)
    assert free_entitlements.outbound is True
    assert outbound_usage(free_owner, now=now)["limits"] == {
        "minute": 2,
        "day": 10,
        "domain_day": 10,
        "month": 30,
    }
    assert outbound_usage(owner, now=now)["limits"] == {
        "minute": 30,
        "day": 500,
        "domain_day": 200,
        "month": 5000,
    }

    control = set_outbound_paused(free_owner, paused=True)
    assert control.is_paused is True
    assert set_outbound_paused(free_owner, paused=False).is_paused is False


@pytest.mark.django_db
@override_settings(
    OUTBOUND_RATE_LIMIT_PER_MINUTE=30,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=500,
    OUTBOUND_DAILY_DOMAIN_LIMIT=200,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=5000,
)
def test_outbound_usage_counts_attempt_rows_once_and_exposes_exact_month_reset(
    owner, project, conversation, inbound_message
):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    _create_attempts(
        owner=owner,
        project=project,
        conversation=conversation,
        inbound_message=inbound_message,
        created_at=[
            datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            now - timedelta(hours=2),
            now - timedelta(seconds=30),
        ],
    )

    first = outbound_usage(owner, now=now)
    second = outbound_usage(owner, now=now)

    assert first == second
    assert first["minute"] == 1
    assert first["day"] == 2
    assert first["month"] == 3
    assert first["by_domain"] == {str(project.id): 2}
    assert first["month_reset_at"] == datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.django_db
@override_settings(
    OUTBOUND_RATE_LIMIT_PER_MINUTE=2,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=100,
    OUTBOUND_DAILY_DOMAIN_LIMIT=100,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=100,
)
def test_minute_limit_returns_stable_retry_metadata(owner, project, conversation, inbound_message):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    _create_attempts(
        owner=owner,
        project=project,
        conversation=conversation,
        inbound_message=inbound_message,
        created_at=[now - timedelta(seconds=45), now - timedelta(seconds=10)],
    )

    with pytest.raises(ValidationError) as caught:
        require_outbound_capacity(project, now=now)

    error = caught.value.error_list[0]
    assert error.code == "outbound_rate_limited"
    assert error.params == {
        "resource": "outbound_replies",
        "used": 2,
        "limit": 2,
        "scope": "account",
        "period": "rolling_minute",
        "retry_after": 15,
    }


@pytest.mark.django_db
@override_settings(
    OUTBOUND_RATE_LIMIT_PER_MINUTE=0,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=2,
    OUTBOUND_DAILY_DOMAIN_LIMIT=100,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=100,
)
def test_rolling_account_limit_returns_exact_reset_at(
    owner, project, conversation, inbound_message
):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    _create_attempts(
        owner=owner,
        project=project,
        conversation=conversation,
        inbound_message=inbound_message,
        created_at=[now - timedelta(hours=23), now - timedelta(hours=2)],
    )

    with pytest.raises(ValidationError) as caught:
        require_outbound_capacity(project, now=now)

    error = caught.value.error_list[0]
    assert error.code == "outbound_account_limit"
    assert error.params == {
        "resource": "outbound_replies",
        "used": 2,
        "limit": 2,
        "scope": "account",
        "period": "rolling_24_hours",
        "reset_at": "2026-08-11T13:00:00Z",
    }


@pytest.mark.django_db
@override_settings(
    OUTBOUND_RATE_LIMIT_PER_MINUTE=0,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=100,
    OUTBOUND_DAILY_DOMAIN_LIMIT=2,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=100,
)
def test_rolling_domain_limit_returns_exact_reset_at(owner, project, conversation, inbound_message):
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    _create_attempts(
        owner=owner,
        project=project,
        conversation=conversation,
        inbound_message=inbound_message,
        created_at=[now - timedelta(hours=20), now - timedelta(hours=1)],
    )

    with pytest.raises(ValidationError) as caught:
        require_outbound_capacity(project, now=now)

    error = caught.value.error_list[0]
    assert error.code == "outbound_domain_limit"
    assert error.params == {
        "resource": "outbound_replies",
        "used": 2,
        "limit": 2,
        "scope": "domain",
        "period": "rolling_24_hours",
        "reset_at": "2026-08-11T16:00:00Z",
    }


@pytest.mark.django_db
@override_settings(
    OUTBOUND_RATE_LIMIT_PER_MINUTE=0,
    OUTBOUND_DAILY_ACCOUNT_LIMIT=0,
    OUTBOUND_DAILY_DOMAIN_LIMIT=0,
    OUTBOUND_MONTHLY_ACCOUNT_LIMIT=2,
)
def test_monthly_limit_resets_at_next_utc_calendar_month(
    owner, project, conversation, inbound_message
):
    now = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
    _create_attempts(
        owner=owner,
        project=project,
        conversation=conversation,
        inbound_message=inbound_message,
        created_at=[
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 20, tzinfo=UTC),
        ],
    )

    with pytest.raises(ValidationError) as caught:
        require_outbound_capacity(project, now=now)

    error = caught.value.error_list[0]
    assert error.code == "outbound_monthly_limit"
    assert error.params == {
        "resource": "outbound_replies",
        "used": 2,
        "limit": 2,
        "scope": "account",
        "period": "calendar_month",
        "reset_at": "2026-09-01T00:00:00Z",
    }
