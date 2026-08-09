from django.urls import path

from .views import (
    OperationalInboxAuthorizationView,
    OperationalInboxOAuthServerMetadataView,
    PublicClientRegistrationView,
    SerializedRevokeTokenView,
    SerializedTokenView,
)

app_name = "oauth2_provider"

urlpatterns = [
    path(
        ".well-known/oauth-authorization-server",
        OperationalInboxOAuthServerMetadataView.as_view(),
        name="oauth-server-metadata",
    ),
    path("oauth/authorize/", OperationalInboxAuthorizationView.as_view(), name="authorize"),
    path("oauth/token/", SerializedTokenView.as_view(), name="token"),
    path("oauth/revoke/", SerializedRevokeTokenView.as_view(), name="revoke-token"),
    path("oauth/register/", PublicClientRegistrationView.as_view(), name="dcr-register"),
]
