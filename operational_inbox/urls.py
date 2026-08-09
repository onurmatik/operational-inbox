from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from inbox import views
from inbox.api import api
from inbox.mcp_server import mcp_endpoint

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", views.login_redirect, name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path(
        "accounts/sesame/login/",
        views.OperationalInboxSesameLoginView.as_view(),
        name="sesame_login",
    ),
    path("api/v1/", api.urls),
    path("mcp", mcp_endpoint, name="mcp"),
    path("health/live", views.health_live, name="health_live"),
    path("health/ready", views.health_ready, name="health_ready"),
    path("", include("inbox.urls")),
]
