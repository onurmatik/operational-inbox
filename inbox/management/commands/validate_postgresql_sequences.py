from __future__ import annotations

import secrets

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from inbox.database_validation import postgresql_sequence_status


class Command(BaseCommand):
    help = "Validate every Django-managed PostgreSQL AutoField sequence."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("The configured database is not PostgreSQL.")
        try:
            statuses = postgresql_sequence_status()
            group = Group.objects.create(name=f"sequence-check-{secrets.token_hex(12)}")
            group.delete()
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Validated {len(statuses)} PostgreSQL sequences and an insert.")
        )
