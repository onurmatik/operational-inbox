from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0006_recover_existing_ses_identities"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="actor_type",
            field=models.CharField(
                choices=[
                    ("OWNER", "Owner"),
                    ("SYSTEM", "System"),
                    ("AGENT", "Agent"),
                    ("AWS", "Operational Inbox"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="domain",
            name="setup_mode",
            field=models.CharField(
                choices=[
                    ("DIRECT_MX", "Direct routing to Operational Inbox"),
                    ("PROVIDER_FORWARD", "Provider catch-all forwarding"),
                ],
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="domaindnsrecord",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("OWNERSHIP", "Ownership"),
                    ("SES_VERIFICATION", "Operational Inbox verification"),
                    ("MX", "Mail exchange"),
                    ("DKIM", "DKIM"),
                    ("SPF", "SPF"),
                    ("DMARC", "DMARC"),
                ],
                max_length=16,
            ),
        ),
    ]
