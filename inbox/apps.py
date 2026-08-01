from __future__ import annotations

from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite(sender: object, connection: object, **kwargs: object) -> None:
    if getattr(connection, "vendor", None) != "sqlite":
        return
    cursor = connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=20000;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")


class InboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inbox"

    def ready(self) -> None:
        connection_created.connect(configure_sqlite, dispatch_uid="inbox.configure_sqlite")
