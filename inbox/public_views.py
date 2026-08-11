from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import render

from inbox.integration_versions import AGENT_MANIFEST

PLUGIN_ROOT = Path(settings.BASE_DIR) / "plugins" / "operational-inbox"
INSTALL_INSTRUCTIONS_URL = (
    "https://raw.githubusercontent.com/onurmatik/operational-inbox/refs/heads/main/INSTALL.md"
)


def privacy(request: HttpRequest) -> HttpResponse:
    return render(request, "public/privacy.html", {"title": "Privacy Policy"})


def terms(request: HttpRequest) -> HttpResponse:
    return render(request, "public/terms.html", {"title": "Terms of Service"})


def support(request: HttpRequest) -> HttpResponse:
    return render(request, "public/support.html", {"title": "Support"})


def mcp_docs(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "public/mcp_docs.html",
        {
            "title": "Agent access and MCP",
            "mcp_resource_url": settings.MCP_RESOURCE_URL,
            "oauth_issuer": settings.OAUTH_ISSUER,
        },
    )


def protected_resource_metadata(request: HttpRequest) -> JsonResponse:
    response = JsonResponse(
        {
            "resource": settings.MCP_RESOURCE_URL,
            "authorization_servers": [settings.OAUTH_ISSUER],
            "scopes_supported": settings.MCP_REQUIRED_SCOPES,
            "bearer_methods_supported": ["header"],
            "resource_documentation": settings.MCP_DOCUMENTATION_URL,
        }
    )
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=3600"
    return response


def agent_manifest(request: HttpRequest) -> JsonResponse:
    response = JsonResponse(AGENT_MANIFEST)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=300"
    return response


def portable_plugin_manifest(request: HttpRequest) -> JsonResponse:
    return _json_file_response(PLUGIN_ROOT / "plugin.json")


def portable_mcp_manifest(request: HttpRequest) -> JsonResponse:
    return _json_file_response(PLUGIN_ROOT / "mcp.json")


def install_instructions(request: HttpRequest) -> HttpResponseRedirect:
    response = HttpResponseRedirect(INSTALL_INSTRUCTIONS_URL)
    response["Cache-Control"] = "public, max-age=300"
    return response


def plugin_logo(request: HttpRequest) -> FileResponse:
    path = PLUGIN_ROOT / "assets" / "logo.png"
    if not path.is_file():
        raise Http404
    response = FileResponse(path.open("rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    return response


def openai_apps_challenge(request: HttpRequest) -> HttpResponse:
    token = settings.OPENAI_APPS_CHALLENGE_TOKEN
    if not token:
        raise Http404
    response = HttpResponse(token, content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response


def _json_file_response(path: Path) -> JsonResponse:
    if not path.is_file():
        raise Http404
    value = json.loads(path.read_text(encoding="utf-8"))
    response = JsonResponse(value)
    response["Access-Control-Allow-Origin"] = "*"
    response["Cache-Control"] = "public, max-age=300"
    return response
