from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from inbox.database_validation import build_database_manifest


class Command(BaseCommand):
    help = "Emit deterministic model counts and identity hashes for database migration checks."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(build_database_manifest(), sort_keys=True))
