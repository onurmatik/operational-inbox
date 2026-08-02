from django.db import migrations, models


def backfill_outbound_status(apps, schema_editor):
    Domain = apps.get_model("inbox", "Domain")
    DomainDNSRecord = apps.get_model("inbox", "DomainDNSRecord")

    for domain in Domain.objects.all().iterator():
        if domain.status == "DISABLED":
            outbound_status = "DISABLED"
        elif domain.outbound_ready:
            outbound_status = "READY"
        else:
            outbound_status = "DISABLED"
            # Older releases generated optional DKIM instructions for every
            # domain, so their presence does not prove that sending was ever
            # requested. Remove those generated instructions and let the new
            # explicit enable flow create fresh ones when needed.
            DomainDNSRecord.objects.filter(domain_id=domain.id, purpose="DKIM").delete()
            if domain.setup_mode == "PROVIDER_FORWARD":
                DomainDNSRecord.objects.filter(
                    domain_id=domain.id, purpose="SES_VERIFICATION"
                ).delete()
        Domain.objects.filter(id=domain.id).update(
            outbound_status=outbound_status,
            outbound_ready=outbound_status == "READY",
        )


class Migration(migrations.Migration):
    dependencies = [("inbox", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="domain",
            name="outbound_error_code",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="domain",
            name="outbound_error_message",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="domain",
            name="outbound_status",
            field=models.CharField(
                choices=[
                    ("DISABLED", "Not enabled"),
                    ("PROVISIONING", "Provisioning"),
                    ("PENDING_DNS", "Pending DNS"),
                    ("READY", "Ready"),
                    ("ERROR", "Error"),
                    ("DEGRADED", "Degraded"),
                ],
                default="DISABLED",
                max_length=24,
            ),
        ),
        migrations.RunPython(backfill_outbound_status, migrations.RunPython.noop),
    ]
