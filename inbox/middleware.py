from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="system")


class RequestIDMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = (
            incoming if 0 < len(incoming) <= 64 and incoming.isascii() else str(uuid.uuid4())
        )
        request.request_id = request_id  # type: ignore[attr-defined]
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)
