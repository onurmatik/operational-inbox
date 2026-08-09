from django.core.management.base import BaseCommand

from oauth_server.cleanup import clear_stale_dynamic_clients


class Command(BaseCommand):
    help = "Delete stale, unused dynamically registered OAuth clients."

    def handle(self, *args, **options):
        deleted = clear_stale_dynamic_clients()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} stale OAuth client row(s)."))
