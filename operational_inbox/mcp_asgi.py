from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "operational_inbox.settings")
django.setup()

from inbox.mcp_application import create_mcp_application  # noqa: E402

application = create_mcp_application()
