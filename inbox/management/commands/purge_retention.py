import json

from django.core.management.base import BaseCommand

from inbox.services.retention import purge_retention


class Command(BaseCommand):
    help = "Apply organization retention policies to S3 and normalized database content."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(purge_retention(), sort_keys=True))
