from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0002_split_outbound_provisioning"),
    ]

    operations = [
        migrations.AddField(
            model_name="domain",
            name="inbound_setup_generation",
            field=models.PositiveBigIntegerField(default=1),
        ),
    ]
