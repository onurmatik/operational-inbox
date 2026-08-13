from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from inbox.database_validation import build_database_manifest


class Command(BaseCommand):
    help = "Compare the configured database with a database_manifest artifact."

    def add_arguments(self, parser):
        parser.add_argument("manifest", type=Path)

    def handle(self, *args, **options):
        expected = json.loads(options["manifest"].read_text())
        actual = build_database_manifest()
        expected["vendor"] = actual["vendor"]
        if actual != expected:
            mismatches = sorted(
                label
                for label in set(expected.get("models", {})) | set(actual.get("models", {}))
                if expected.get("models", {}).get(label) != actual.get("models", {}).get(label)
            )
            raise CommandError(
                "Database manifest mismatch: " + ", ".join(mismatches or ["metadata"])
            )
        self.stdout.write(self.style.SUCCESS("Database manifest matches."))
