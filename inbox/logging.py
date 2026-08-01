from __future__ import annotations

import logging

from inbox.middleware import request_id_var


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
