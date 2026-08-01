import json

from django.core.management.base import BaseCommand

from inbox.services.receipt_rules import reconcile_receipt_rule


class Command(BaseCommand):
    help = "Reconcile the single explicit-recipient SES receipt rule."

    def handle(self, *args, **options):
        result = reconcile_receipt_rule()
        self.stdout.write(
            json.dumps(
                {"action": result.action, "recipient_count": len(result.recipients)},
                sort_keys=True,
            )
        )
