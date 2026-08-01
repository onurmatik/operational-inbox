from django.contrib import admin
from django.urls import include, path

from inbox import views
from inbox.api import api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("api/v1/", api.urls),
    path("health/live", views.health_live, name="health_live"),
    path("health/ready", views.health_ready, name="health_ready"),
    path("", include("inbox.urls")),
]
