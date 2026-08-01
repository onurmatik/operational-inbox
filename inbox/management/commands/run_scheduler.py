import json

from django.core.management.base import BaseCommand, CommandParser

from inbox.services.jobs import run_due_jobs, schedule_work


class Command(BaseCommand):
    help = "Enqueue due reviews, reports, notifications, and sends, then process durable jobs."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--max-jobs", type=int, default=100)

    def handle(self, *args, **options):
        scheduled = schedule_work()
        counts = run_due_jobs(limit=options["max_jobs"])
        self.stdout.write(json.dumps({"scheduled": scheduled, **counts}, sort_keys=True))
