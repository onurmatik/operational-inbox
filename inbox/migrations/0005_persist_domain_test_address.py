from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def expire_legacy_pending_tests(apps, schema_editor):
    DomainTest = apps.get_model("inbox", "DomainTest")
    DomainTest.objects.using(schema_editor.connection.alias).filter(status="PENDING").update(
        status="EXPIRED",
        updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0004_staged_routing_transitions"),
    ]

    operations = [
        migrations.AddField(
            model_name="domaintest",
            name="address",
            field=models.CharField(blank=True, max_length=320, null=True, unique=True),
        ),
        migrations.RunPython(expire_legacy_pending_tests, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="domaintest",
            constraint=models.UniqueConstraint(
                condition=Q(status="PENDING"),
                fields=(
                    "domain",
                    "setup_generation",
                    "expected_setup_mode",
                    "expected_route_kind",
                ),
                name="uniq_pending_domain_test_scope",
            ),
        ),
        migrations.AddIndex(
            model_name="domaintest",
            index=models.Index(
                fields=("domain", "status", "setup_generation", "expires_at"),
                name="domain_test_scope_lookup",
            ),
        ),
    ]
