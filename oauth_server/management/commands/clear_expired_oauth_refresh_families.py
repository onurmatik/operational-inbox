from django.core.management.base import BaseCommand

from oauth_server.cleanup import clear_expired_refresh_families


class Command(BaseCommand):
    help = "Delete expired OAuth refresh-family records after their tokens are gone."

    def handle(self, *args, **options):
        deleted = clear_expired_refresh_families()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted} expired OAuth refresh-family row(s).")
        )
