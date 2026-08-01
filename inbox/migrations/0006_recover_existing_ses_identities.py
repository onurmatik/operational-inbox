from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def prepare_existing_identities(apps, schema_editor):
    Domain = apps.get_model("inbox", "Domain")
    DomainDNSRecord = apps.get_model("inbox", "DomainDNSRecord")
    DurableJob = apps.get_model("inbox", "DurableJob")
    AuditEvent = apps.get_model("inbox", "AuditEvent")

    # Older retries could retain more than one value for the same DNS target.
    # Keep the newest generated instruction before tightening uniqueness.
    # Drift checks also update ``updated_at``, so it cannot identify token age.
    duplicate_targets = (
        DomainDNSRecord.objects.values("domain_id", "purpose", "record_type", "name")
        .annotate(record_count=Count("id"))
        .filter(record_count__gt=1)
    )
    for target in duplicate_targets:
        records = DomainDNSRecord.objects.filter(
            domain_id=target["domain_id"],
            purpose=target["purpose"],
            record_type=target["record_type"],
            name=target["name"],
        ).order_by("-created_at", "-id")
        keep_id = records.values_list("id", flat=True).first()
        records.exclude(id=keep_id).delete()

    # Preserve provenance separately from the mutable SES observation field.
    managed_domain_ids = DomainDNSRecord.objects.values_list("domain_id", flat=True).distinct()
    Domain.objects.filter(id__in=managed_domain_ids).update(ses_identity_origin="MANAGED")
    Domain.objects.filter(ses_identity_status__in=["MANAGED", "PROVISIONING"]).update(
        ses_identity_origin="MANAGED"
    )

    # Legacy releases terminally rejected any pre-existing SES identity. The
    # new worker performs a read-only adoption and issues a fresh, claim-bound
    # application TXT challenge before enabling routing or sending.
    now = timezone.now()
    collisions = list(Domain.objects.filter(status="ERROR", error_code="ses_identity_collision"))
    for domain in collisions:
        Domain.objects.filter(id=domain.id).update(
            status="PROVISIONING",
            inbound_ready=False,
            outbound_ready=False,
            error_code="",
            error_message="",
            updated_at=now,
        )
        DurableJob.objects.get_or_create(
            idempotency_key=f"provision-domain:{domain.id}:existing-identity-recovery",
            defaults={
                "organization_id": domain.organization_id,
                "kind": "provision_domain",
                "payload": {"domain_id": str(domain.id)},
                "due_at": now,
            },
        )
        AuditEvent.objects.get_or_create(
            organization_id=domain.organization_id,
            actor_type="SYSTEM",
            event_type="domain.provision_recovery_scheduled",
            object_type="Domain",
            object_id=domain.id,
            request_id="migration:0006",
            defaults={"metadata": {"previous_error_code": "ses_identity_collision"}},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0005_remove_signupattempt_inbox_signu_fingerp_c51b87_idx_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="domaindnsrecord",
            name="uniq_domain_dns_instruction",
        ),
        migrations.AddField(
            model_name="domain",
            name="ses_identity_origin",
            field=models.CharField(
                blank=True,
                choices=[
                    ("MANAGED", "Created by Operational Inbox"),
                    ("ADOPTION_PENDING", "Existing identity; ownership pending"),
                    ("ADOPTED", "Existing identity; ownership verified"),
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
                    ("SES_VERIFICATION", "SES verification"),
                    ("MX", "Mail exchange"),
                    ("DKIM", "DKIM"),
                    ("SPF", "SPF"),
                    ("DMARC", "DMARC"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(prepare_existing_identities, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="domaindnsrecord",
            constraint=models.UniqueConstraint(
                fields=("domain", "purpose", "record_type", "name"),
                name="uniq_domain_dns_instruction_target",
            ),
        ),
    ]
