from django.db import migrations, models
from django.utils import timezone


def keep_one_active_token_per_owner(apps, schema_editor):
    APIToken = apps.get_model("inbox", "APIToken")
    now = timezone.now()
    owner_ids = APIToken.objects.filter(revoked_at__isnull=True).values_list(
        "owner_id", flat=True
    ).distinct()
    for owner_id in owner_ids.iterator():
        active_ids = list(
            APIToken.objects.filter(owner_id=owner_id, revoked_at__isnull=True)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )
        APIToken.objects.filter(id__in=active_ids[1:]).update(
            revoked_at=now,
            updated_at=now,
        )


class Migration(migrations.Migration):
    dependencies = [("inbox", "0010_agent_delegated_outbound")]

    operations = [
        migrations.RunPython(keep_one_active_token_per_owner, migrations.RunPython.noop),
        migrations.RemoveField(model_name="apitoken", name="domain"),
        migrations.RemoveField(model_name="apitoken", name="name"),
        migrations.RemoveField(model_name="apitoken", name="scopes"),
        migrations.AddConstraint(
            model_name="apitoken",
            constraint=models.UniqueConstraint(
                condition=models.Q(("revoked_at__isnull", True)),
                fields=("owner",),
                name="uniq_active_api_token_per_owner",
            ),
        ),
    ]
