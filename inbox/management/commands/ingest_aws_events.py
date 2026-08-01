import json

from django.core.management.base import BaseCommand, CommandParser

from inbox.services.ingestion import consume_queue


class Command(BaseCommand):
    help = "Long-poll AWS SQS and ingest SES receipt and delivery notifications."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-runtime", type=int, default=55)

    def handle(self, *args, **options):
        counts = consume_queue(max_runtime=options["max_runtime"])
        self.stdout.write(json.dumps(counts, sort_keys=True))
