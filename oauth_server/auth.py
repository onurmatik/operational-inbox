from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.conf import settings
from oauth2_provider.models import get_access_token_model

from inbox.models import User


@dataclass(frozen=True)
class OAuthAccess:
    user: User
    client_id: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def verify_oauth_access_token(raw_token: str) -> OAuthAccess | None:
    """Validate a local opaque access token against its checksum and exact resource."""

    if not raw_token:
        return None
    checksum = hashlib.sha256(raw_token.encode()).hexdigest()
    token_model = get_access_token_model()
    stored = (
        token_model.objects.select_related("application", "user")
        .filter(token_checksum=checksum)
        .first()
    )
    if stored is None or stored.user is None or not stored.user.is_active:
        return None
    if not stored.is_valid():
        return None
    if list(stored.resource or []) != [settings.MCP_RESOURCE_URL]:
        return None
    application = stored.application
    if (
        application is None
        or application.client_type != application.CLIENT_PUBLIC
        or application.authorization_grant_type != application.GRANT_AUTHORIZATION_CODE
    ):
        return None
    return OAuthAccess(
        user=stored.user,
        client_id=application.client_id,
        scopes=frozenset(stored.scopes),
    )
