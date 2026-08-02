import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_routing_metadata(apps, schema_editor):
    Domain = apps.get_model("inbox", "Domain")
    DomainTest = apps.get_model("inbox", "DomainTest")
    InboundRoute = apps.get_model("inbox", "InboundRoute")
    database_alias = schema_editor.connection.alias

    domains = Domain.objects.using(database_alias).only(
        "id",
        "inbound_setup_generation",
        "setup_mode",
    )
    for domain in domains.iterator():
        generation = domain.inbound_setup_generation
        expected_route_kind = (
            "DIRECT_DOMAIN" if domain.setup_mode == "DIRECT_MX" else "FORWARDING_ALIAS"
        )
        InboundRoute.objects.using(database_alias).filter(domain_id=domain.id).update(
            setup_generation=generation,
        )
        DomainTest.objects.using(database_alias).filter(domain_id=domain.id).update(
            setup_generation=generation,
            expected_setup_mode=domain.setup_mode,
            expected_route_kind=expected_route_kind,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0003_domain_inbound_setup_generation"),
    ]

    operations = [
        migrations.CreateModel(
            name="InboundRoutingTransition",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("generation", models.PositiveBigIntegerField()),
                (
                    "from_mode",
                    models.CharField(
                        choices=[
                            ("DIRECT_MX", "Direct routing to Operational Inbox"),
                            ("PROVIDER_FORWARD", "Provider catch-all forwarding"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "to_mode",
                    models.CharField(
                        choices=[
                            ("DIRECT_MX", "Direct routing to Operational Inbox"),
                            ("PROVIDER_FORWARD", "Provider catch-all forwarding"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "from_domain_status",
                    models.CharField(
                        choices=[
                            ("PROVISIONING", "Provisioning"),
                            ("PENDING_DNS", "Pending DNS"),
                            ("PENDING_TEST", "Pending test delivery"),
                            ("READY", "Ready"),
                            ("ERROR", "Error"),
                            ("DEGRADED", "Degraded"),
                            ("DISABLED", "Disabled"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PREPARING", "Preparing"),
                            ("WAITING_DNS", "Waiting for DNS"),
                            ("WAITING_TEST", "Waiting for test delivery"),
                            ("GRACE", "Grace period"),
                            ("COMPLETE", "Complete"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PREPARING",
                        max_length=16,
                    ),
                ),
                ("dns_verified_at", models.DateTimeField(blank=True, null=True)),
                ("test_received_at", models.DateTimeField(blank=True, null=True)),
                ("cutover_at", models.DateTimeField(blank=True, null=True)),
                ("grace_until", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("error_message", models.CharField(blank=True, max_length=240)),
                (
                    "domain",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="routing_transitions",
                        to="inbox.domain",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("domain", "generation"),
                        name="uniq_domain_routing_transition_generation",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(
                            status__in=(
                                "PREPARING",
                                "WAITING_DNS",
                                "WAITING_TEST",
                                "GRACE",
                                "FAILED",
                            )
                        ),
                        fields=("domain",),
                        name="uniq_active_domain_routing_transition",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="inboundroute",
            name="grace_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="inboundroute",
            name="routing_transition",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="routes",
                to="inbox.inboundroutingtransition",
            ),
        ),
        migrations.AddField(
            model_name="inboundroute",
            name="setup_generation",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="domaintest",
            name="expected_route_kind",
            field=models.CharField(
                choices=[
                    ("DIRECT_DOMAIN", "Direct domain"),
                    ("FORWARDING_ALIAS", "Forwarding alias"),
                    ("TEST", "Test delivery"),
                ],
                default="DIRECT_DOMAIN",
                max_length=24,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="domaintest",
            name="expected_setup_mode",
            field=models.CharField(
                choices=[
                    ("DIRECT_MX", "Direct routing to Operational Inbox"),
                    ("PROVIDER_FORWARD", "Provider catch-all forwarding"),
                ],
                default="DIRECT_MX",
                max_length=24,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="domaintest",
            name="routing_transition",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tests",
                to="inbox.inboundroutingtransition",
            ),
        ),
        migrations.AddField(
            model_name="domaintest",
            name="setup_generation",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.RunPython(
            backfill_routing_metadata,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="domaintest",
            name="expected_route_kind",
            field=models.CharField(
                choices=[
                    ("DIRECT_DOMAIN", "Direct domain"),
                    ("FORWARDING_ALIAS", "Forwarding alias"),
                    ("TEST", "Test delivery"),
                ],
                default="DIRECT_DOMAIN",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="domaintest",
            name="expected_setup_mode",
            field=models.CharField(
                choices=[
                    ("DIRECT_MX", "Direct routing to Operational Inbox"),
                    ("PROVIDER_FORWARD", "Provider catch-all forwarding"),
                ],
                default="DIRECT_MX",
                max_length=24,
            ),
        ),
    ]
