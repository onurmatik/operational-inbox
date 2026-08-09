from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models
from oauth2_provider.models import AbstractApplication

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class OAuthApplication(AbstractApplication):
    """OAuth client constrained to Operational Inbox public PKCE flows."""

    class Meta(AbstractApplication.Meta):
        swappable = "OAUTH2_PROVIDER_APPLICATION_MODEL"

    def get_allowed_schemes(self):
        if any(_is_loopback_http_uri(uri) for uri in self.redirect_uris.split()):
            return ["https", "http"]
        return ["https"]

    def save(self, *args, **kwargs):
        if self.client_type == self.CLIENT_PUBLIC:
            self.client_secret = ""
            self.hash_client_secret = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}
        if self.client_type != self.CLIENT_PUBLIC:
            errors["client_type"] = "Only public OAuth clients are supported."
        if self.authorization_grant_type != self.GRANT_AUTHORIZATION_CODE:
            errors["authorization_grant_type"] = "Only the authorization code grant is supported."
        if self.skip_authorization:
            errors["skip_authorization"] = (
                "Operational Inbox always requires explicit user consent."
            )
        if self.algorithm:
            errors["algorithm"] = "OIDC signing algorithms are not supported."

        for uri in self.redirect_uris.split():
            parsed = urlsplit(uri)
            if parsed.fragment:
                errors["redirect_uris"] = "Redirect URIs must not contain fragments."
                break
            if parsed.username or parsed.password:
                errors["redirect_uris"] = "Redirect URIs must not contain user information."
                break
            if parsed.scheme == "http" and not _is_loopback_http_uri(uri):
                errors["redirect_uris"] = (
                    "HTTP redirect URIs are allowed only for loopback callbacks."
                )
                break
            if parsed.scheme not in {"https", "http"}:
                errors["redirect_uris"] = "Redirect URIs must use HTTPS or loopback HTTP."
                break

        if errors:
            raise ValidationError(errors)


class OAuthRefreshFamily(models.Model):
    """Persist the absolute lifetime of a rotating refresh-token family."""

    family_id = models.UUIDField(primary_key=True, editable=False)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)


def _is_loopback_http_uri(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in LOOPBACK_HOSTS
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )
