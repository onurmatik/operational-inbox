from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0011_global_api_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="billingprofile",
            name="domain_grace_ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="billingprofile",
            name="free_primary_domain",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="inbox.domain",
            ),
        ),
    ]
