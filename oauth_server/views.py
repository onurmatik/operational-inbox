import json
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider.compat import login_not_required
from oauth2_provider.models import get_application_model
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import (
    AuthorizationView,
    OAuthServerMetadataView,
    RevokeTokenView,
    TokenView,
)
from oauth2_provider.views.dynamic_client_registration import (
    _build_application_kwargs,
    _check_permissions,
    _dot_grant_to_rfc_grant_types,
    _error_response,
    _parse_metadata,
    _validation_error_description,
)


class OperationalInboxOAuthServerMetadataView(OAuthServerMetadataView):
    """Publish OAuth metadata compatible with current agent clients."""

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        metadata = json.loads(response.content.decode(response.charset))
        # Codex currently drops `iss` before validating the authorization response.
        # Keep emitting `iss`, but do not advertise it as required until the client fixes this.
        metadata["authorization_response_iss_parameter_supported"] = False
        response.content = json.dumps(metadata, separators=(",", ":")).encode(response.charset)
        return response


class OperationalInboxAuthorizationView(AuthorizationView):
    """Consent endpoint that accepts only the Operational Inbox MCP resource."""

    def get(self, request, *args, **kwargs):
        if request.GET.getlist("resource") != [settings.MCP_RESOURCE_URL]:
            return _invalid_target_response()
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        resources = (form.cleaned_data.get("resource") or "").split()
        if resources != [settings.MCP_RESOURCE_URL]:
            return _invalid_target_response()
        return super().form_valid(form)

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        add_never_cache_headers(response)
        return response


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class SerializedTokenView(TokenView):
    """Serialize code redemption and refresh rotation for SQLite."""

    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            response = super().post(request, *args, **kwargs)
        response["Content-Type"] = "application/json"
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class SerializedRevokeTokenView(RevokeTokenView):
    def post(self, request, *args, **kwargs):
        with transaction.atomic():
            return super().post(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class PublicClientRegistrationView(View):
    """Register public PKCE clients without an RFC 7592 management token."""

    MAX_BODY_BYTES = 16 * 1024

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response

    def post(self, request, *args, **kwargs):
        if not oauth2_settings.DCR_ENABLED:
            return JsonResponse({"error": "not_found"}, status=404)
        if len(request.body) > self.MAX_BODY_BYTES:
            return _error_response(
                "invalid_client_metadata",
                "Registration metadata is too large.",
            )
        if not _check_permissions(request):
            response = _error_response(
                "access_denied",
                "Client registration rate limit exceeded.",
                status=429,
            )
            response["Retry-After"] = "3600"
            return response

        data, error = _parse_metadata(request.body)
        if error:
            return error
        policy_error = _validate_public_client_metadata(data)
        if policy_error:
            return policy_error
        application_kwargs, error = _build_application_kwargs(data)
        if error:
            return error

        application_model = get_application_model()
        application = application_model(
            user=None,
            registration_source=application_model.RegistrationSource.DCR,
            **application_kwargs,
        )
        try:
            application.full_clean()
        except ValidationError as exc:
            return _error_response(
                "invalid_client_metadata",
                _validation_error_description(exc),
            )
        since = timezone.now() - timedelta(hours=1)
        with transaction.atomic():
            if (
                application_model.objects.filter(
                    registration_source=application_model.RegistrationSource.DCR,
                    created__gte=since,
                ).count()
                >= settings.OAUTH_DCR_GLOBAL_HOURLY_LIMIT
            ):
                response = _error_response(
                    "access_denied",
                    "Client registration is temporarily at capacity.",
                    status=429,
                )
                response["Retry-After"] = "3600"
                return response
            application.save()

        return JsonResponse(
            {
                "client_id": application.client_id,
                "client_id_issued_at": int(application.created.timestamp()),
                "client_name": application.name,
                "redirect_uris": application.redirect_uris.split(),
                "response_types": ["code"],
                "grant_types": _dot_grant_to_rfc_grant_types(application.authorization_grant_type),
                "token_endpoint_auth_method": "none",
            },
            status=201,
        )


def _validate_public_client_metadata(data):
    client_name = data.get("client_name", "")
    if not isinstance(client_name, str) or len(client_name) > 255:
        return _error_response(
            "invalid_client_metadata",
            "client_name must be a string no longer than 255 characters.",
        )
    if data.get("token_endpoint_auth_method", "none") != "none":
        return _error_response(
            "invalid_client_metadata",
            'Only public clients with token_endpoint_auth_method "none" are supported.',
        )
    grant_types = data.get("grant_types", ["authorization_code", "refresh_token"])
    if (
        not isinstance(grant_types, list)
        or not all(isinstance(value, str) for value in grant_types)
        or set(grant_types) not in ({"authorization_code"}, {"authorization_code", "refresh_token"})
    ):
        return _error_response(
            "invalid_client_metadata",
            "Only authorization_code with optional refresh_token is supported.",
        )
    response_types = data.get("response_types", ["code"])
    if not isinstance(response_types, list) or response_types != ["code"]:
        return _error_response(
            "invalid_client_metadata",
            'Only response_type "code" is supported.',
        )
    redirect_uris = data.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > settings.OAUTH_DCR_MAX_REDIRECT_URIS
        or not all(isinstance(value, str) for value in redirect_uris)
    ):
        return _error_response(
            "invalid_client_metadata",
            "redirect_uris must contain between 1 and "
            f"{settings.OAUTH_DCR_MAX_REDIRECT_URIS} values.",
        )
    return None


def _invalid_target_response():
    response = JsonResponse(
        {
            "error": "invalid_target",
            "error_description": (
                "The request must target exactly the Operational Inbox MCP resource."
            ),
        },
        status=400,
    )
    add_never_cache_headers(response)
    return response
