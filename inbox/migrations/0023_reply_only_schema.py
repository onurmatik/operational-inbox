"""Require inbound reply context after Compose deletion has committed.

Keep schema changes in a separate migration so PostgreSQL deferred foreign-key
triggers from the cleanup cannot block ALTER TABLE.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("inbox", "0022_delete_compose_test_data")]
    operations = [
        migrations.RemoveConstraint(model_name="replydraft", name="draft_kind_context"),
        migrations.RemoveField(model_name="replydraft", name="kind"),
        migrations.AlterField(
            model_name="replydraft",
            name="context_message",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reply_drafts",
                to="inbox.message",
            ),
        ),
        migrations.RemoveField(model_name="replydraftrevision", name="from_address"),
        migrations.RemoveField(model_name="replydraftrevision", name="to_address"),
        migrations.RemoveConstraint(
            model_name="outboundmessage", name="uniq_domain_compose_send_key"
        ),
        migrations.AddConstraint(
            model_name="outboundmessage",
            constraint=models.UniqueConstraint(
                fields=("domain", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False),
                name="uniq_domain_send_key",
            ),
        ),
    ]
