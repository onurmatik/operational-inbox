from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Exists, OuterRef
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_grant_model,
    get_refresh_token_model,
)

from oauth_server.models import OAuthRefreshFamily


def clear_expired_refresh_families() -> int:
    refresh_token_model = get_refresh_token_model()
    expired = (
        OAuthRefreshFamily.objects.filter(expires_at__lte=timezone.now())
        .annotate(
            has_refresh_tokens=Exists(
                refresh_token_model.objects.filter(token_family=OuterRef("family_id"))
            )
        )
        .filter(has_refresh_tokens=False)
    )
    deleted, _ = expired.delete()
    return deleted


def clear_stale_dynamic_clients() -> int:
    retention_days = settings.OAUTH_DCR_CLIENT_RETENTION_DAYS
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or retention_days < 1
    ):
        raise ImproperlyConfigured("OAUTH_DCR_CLIENT_RETENTION_DAYS must be a positive integer.")

    application_model = get_application_model()
    access_token_model = get_access_token_model()
    grant_model = get_grant_model()
    refresh_token_model = get_refresh_token_model()
    cutoff = timezone.now() - timedelta(days=retention_days)
    stale = (
        application_model.objects.filter(
            registration_source=application_model.RegistrationSource.DCR,
            updated__lt=cutoff,
        )
        .annotate(
            has_access_tokens=Exists(
                access_token_model.objects.filter(application_id=OuterRef("pk"))
            ),
            has_grants=Exists(grant_model.objects.filter(application_id=OuterRef("pk"))),
            has_refresh_tokens=Exists(
                refresh_token_model.objects.filter(application_id=OuterRef("pk"))
            ),
        )
        .filter(
            has_access_tokens=False,
            has_grants=False,
            has_refresh_tokens=False,
        )
    )
    deleted, _ = stale.delete()
    return deleted
