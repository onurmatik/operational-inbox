import json

from django.core.management.base import BaseCommand
from oauth2_provider.models import clear_expired

from inbox.services.retention import purge_retention
from oauth_server.cleanup import clear_expired_refresh_families, clear_stale_dynamic_clients


class Command(BaseCommand):
    help = "Apply domain retention policies to S3 and normalized database content."

    def handle(self, *args, **options):
        counts = purge_retention()
        clear_expired()
        counts["oauth_refresh_families"] = clear_expired_refresh_families()
        counts["oauth_clients"] = clear_stale_dynamic_clients()
        self.stdout.write(json.dumps(counts, sort_keys=True))
