import uuid

import django.db.models.deletion
from django.db import migrations, models


def migrate_workflow_state_to_tags(apps, schema_editor):
    Conversation = apps.get_model("inbox", "Conversation")
    ConversationTag = apps.get_model("inbox", "ConversationTag")

    tags = []
    for conversation in Conversation.objects.all().iterator():
        if conversation.status == "WAITING_EXTERNAL":
            tags.append(
                ConversationTag(
                    domain_id=conversation.domain_id,
                    conversation_id=conversation.id,
                    name="waiting-external",
                    normalized_name="waiting-external",
                )
            )
        if conversation.work_started_at is not None:
            tags.append(
                ConversationTag(
                    domain_id=conversation.domain_id,
                    conversation_id=conversation.id,
                    name="in-progress",
                    normalized_name="in-progress",
                )
            )
        if (
            conversation.status == "RESOLVED"
            and conversation.archived_at is None
            and conversation.trashed_at is None
        ):
            conversation.archived_at = conversation.resolved_at or conversation.updated_at
            conversation.save(update_fields=("archived_at",))
    ConversationTag.objects.bulk_create(tags, ignore_conflicts=True)


def restore_legacy_workflow_state(apps, schema_editor):
    Conversation = apps.get_model("inbox", "Conversation")
    ConversationTag = apps.get_model("inbox", "ConversationTag")

    for tag in ConversationTag.objects.filter(
        normalized_name__in=("waiting-external", "in-progress")
    ).iterator():
        conversation = Conversation.objects.get(id=tag.conversation_id)
        update_fields = []
        if tag.normalized_name == "waiting-external":
            conversation.status = "WAITING_EXTERNAL"
            update_fields.append("status")
        elif tag.normalized_name == "in-progress":
            conversation.work_started_at = tag.created_at
            update_fields.append("work_started_at")
        if update_fields:
            conversation.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [("inbox", "0008_message_tracking")]

    operations = [
        migrations.CreateModel(
            name="ConversationTag",
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
                ("name", models.CharField(max_length=64)),
                ("normalized_name", models.CharField(max_length=64)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tags",
                        to="inbox.conversation",
                    ),
                ),
                (
                    "domain",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="inbox.domain",
                    ),
                ),
            ],
            options={"ordering": ("normalized_name", "created_at")},
        ),
        migrations.AddConstraint(
            model_name="conversationtag",
            constraint=models.UniqueConstraint(
                fields=("conversation", "normalized_name"),
                name="uniq_conversation_tag_name",
            ),
        ),
        migrations.AddIndex(
            model_name="conversationtag",
            index=models.Index(
                fields=["domain", "normalized_name", "-created_at"],
                name="inbox_tag_domain_name_idx",
            ),
        ),
        migrations.AlterField(
            model_name="apitoken",
            name="domain",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="inbox.domain",
            ),
        ),
        migrations.RunPython(
            migrate_workflow_state_to_tags,
            restore_legacy_workflow_state,
        ),
        migrations.RemoveIndex(
            model_name="conversation",
            name="inbox_conve_domain__eb38cc_idx",
        ),
        migrations.RemoveField(model_name="conversation", name="status"),
        migrations.RemoveField(model_name="conversation", name="resolved_at"),
        migrations.RemoveField(model_name="conversation", name="work_started_at"),
    ]
