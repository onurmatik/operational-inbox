from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import get_refresh_token_model
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauth2_provider.settings import oauth2_settings
from oauthlib.oauth2.rfc6749 import errors

from .models import OAuthRefreshFamily


class OperationalInboxOAuth2Validator(OAuth2Validator):
    """Enforce an exact MCP resource and finite refresh-token lifetime."""

    def _create_authorization_code(self, request, code, expires=None):
        self._require_exact_resource(request)
        return super()._create_authorization_code(request, code, expires=expires)

    def _check_and_set_request_resource(self, request):
        if request.grant_type == "authorization_code":
            submitted = [
                value
                for key, value in (getattr(request, "decoded_body", None) or [])
                if key == "resource"
            ]
            if submitted != [settings.MCP_RESOURCE_URL]:
                raise errors.CustomOAuth2Error(
                    error="invalid_target",
                    description=(
                        "The token request must include exactly the Operational Inbox MCP resource."
                    ),
                    request=request,
                )
        super()._check_and_set_request_resource(request)
        self._require_exact_resource(request)

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        if not super().validate_refresh_token(
            refresh_token,
            client,
            request,
            *args,
            **kwargs,
        ):
            return False
        token = request.refresh_token_instance
        family = _refresh_family_for_token(token)
        if family is not None and family.expires_at <= timezone.now():
            _revoke_refresh_family(token.token_family)
            return False
        return True

    def _create_refresh_token(
        self,
        request,
        refresh_token_code,
        access_token,
        previous_refresh_token,
    ):
        refresh_token = super()._create_refresh_token(
            request,
            refresh_token_code,
            access_token,
            previous_refresh_token,
        )
        _refresh_family_for_token(refresh_token)
        return refresh_token

    @staticmethod
    def _require_exact_resource(request):
        resources = getattr(request, "resource", None)
        resources = [resources] if isinstance(resources, str) else list(resources or [])
        if resources != [settings.MCP_RESOURCE_URL]:
            raise errors.CustomOAuth2Error(
                error="invalid_target",
                description=("The request must target exactly the Operational Inbox MCP resource."),
                request=request,
            )


def exact_resource_validator(request_uri, audiences):
    return request_uri == settings.MCP_RESOURCE_URL and list(audiences or []) == [
        settings.MCP_RESOURCE_URL
    ]


def _refresh_family_for_token(token):
    lifetime = oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS
    if not lifetime or token.token_family is None:
        return None
    if not isinstance(lifetime, timedelta):
        lifetime = timedelta(seconds=lifetime)
    return OAuthRefreshFamily.objects.get_or_create(
        family_id=token.token_family,
        defaults={
            "issued_at": token.created,
            "expires_at": token.created + lifetime,
        },
    )[0]


def _revoke_refresh_family(token_family):
    refresh_token_model = get_refresh_token_model()
    for related_token in refresh_token_model.objects.filter(token_family=token_family):
        related_token.revoke()
