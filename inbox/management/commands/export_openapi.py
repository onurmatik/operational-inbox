from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from inbox.api import api


class Command(BaseCommand):
    help = "Export the public Django Ninja OpenAPI schema as deterministic JSON."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--output",
            type=Path,
            default=Path(settings.BASE_DIR) / "openapi.json",
            help="Output path (default: <project>/openapi.json).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail if the tracked schema is missing or differs from the generated schema.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        output: Path = options["output"]
        rendered = (
            json.dumps(
                api.get_openapi_schema(path_prefix="/api/v1"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        if options["check"]:
            if not output.is_file():
                raise CommandError(f"OpenAPI schema is missing: {output}")
            if output.read_text(encoding="utf-8") != rendered:
                raise CommandError(
                    "OpenAPI schema is out of date. Run "
                    "`python manage.py export_openapi` and commit the result."
                )
            self.stdout.write(self.style.SUCCESS(f"OpenAPI schema is current: {output}"))
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote OpenAPI schema: {output}"))
