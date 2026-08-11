from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from inbox import public_views, views
from inbox.api import api

urlpatterns = [
    path("agent-manifest.json", public_views.agent_manifest, name="agent_manifest"),
    path(".well-known/openai-apps-challenge", public_views.openai_apps_challenge),
    path(
        ".well-known/oauth-protected-resource",
        public_views.protected_resource_metadata,
        name="oauth_protected_resource",
    ),
    path(
        ".well-known/oauth-protected-resource/mcp",
        public_views.protected_resource_metadata,
        name="oauth_protected_resource_mcp",
    ),
    path(".well-known/agent-plugin/plugin.json", public_views.portable_plugin_manifest),
    path(".well-known/agent-plugin/mcp.json", public_views.portable_mcp_manifest),
    path(
        "plugins/operational-inbox/plugin.json",
        public_views.portable_plugin_manifest,
        name="portable_plugin_manifest",
    ),
    path(
        "plugins/operational-inbox/mcp.json",
        public_views.portable_mcp_manifest,
        name="portable_mcp_manifest",
    ),
    path("plugin-assets/logo.png", public_views.plugin_logo, name="plugin_logo"),
    path("INSTALL.md", public_views.install_instructions, name="install_instructions"),
    path("privacy/", public_views.privacy, name="privacy"),
    path("terms/", public_views.terms, name="terms"),
    path("support/", public_views.support, name="support"),
    path("mcp-docs/", public_views.mcp_docs, name="mcp_docs"),
    path("admin/", admin.site.urls),
    path("accounts/login/", views.login_redirect, name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path(
        "accounts/sesame/login/",
        views.OperationalInboxSesameLoginView.as_view(),
        name="sesame_login",
    ),
    path("api/v1/", api.urls),
    path("health/live", views.health_live, name="health_live"),
    path("health/ready", views.health_ready, name="health_ready"),
    path("", include("inbox.urls")),
]

if settings.OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED:
    urlpatterns.insert(0, path("", include("oauth_server.urls")))
