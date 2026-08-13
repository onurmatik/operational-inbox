from __future__ import annotations

import hashlib
from typing import Any

from django.apps import apps
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models


def _digest(values: list[str]) -> str:
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def build_database_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = {"vendor": connection.vendor, "models": {}}
    for model in sorted(apps.get_models(), key=lambda item: item._meta.label_lower):
        if model._meta.proxy or not model._meta.managed:
            continue
        queryset = model._base_manager.using("default")
        if model is ContentType:
            identities = [
                f"{app_label}:{model_name}"
                for app_label, model_name in queryset.order_by("app_label", "model").values_list(
                    "app_label", "model"
                )
            ]
        elif model is Permission:
            identities = [
                f"{app_label}:{model_name}:{codename}"
                for app_label, model_name, codename in queryset.order_by(
                    "content_type__app_label", "content_type__model", "codename"
                ).values_list("content_type__app_label", "content_type__model", "codename")
            ]
        else:
            pk_name = model._meta.pk.name
            identities = [
                str(value)
                for value in queryset.order_by(pk_name).values_list(pk_name, flat=True)
            ]
        manifest["models"][model._meta.label_lower] = {
            "count": len(identities),
            "identity_sha256": _digest(identities),
        }
    return manifest


def postgresql_sequence_status() -> list[dict[str, Any]]:
    if connection.vendor != "postgresql":
        raise RuntimeError("PostgreSQL sequence validation requires a PostgreSQL connection.")
    statuses: list[dict[str, Any]] = []
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        current_user = cursor.fetchone()[0]
        for model in sorted(apps.get_models(), key=lambda item: item._meta.label_lower):
            pk = model._meta.pk
            if (
                model._meta.proxy
                or not model._meta.managed
                or not isinstance(pk, models.AutoField)
            ):
                continue
            table = model._meta.db_table
            column = pk.column
            cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", [table, column])
            sequence = cursor.fetchone()[0]
            if not sequence:
                raise RuntimeError(f"No PostgreSQL sequence found for {table}.{column}.")
            schema_name, sequence_name = sequence.split(".", 1)
            quoted_sequence = ".".join(
                connection.ops.quote_name(part.strip('"'))
                for part in (schema_name, sequence_name)
            )
            quoted_table = connection.ops.quote_name(table)
            quoted_column = connection.ops.quote_name(column)
            cursor.execute(f"SELECT MAX({quoted_column}) FROM {quoted_table}")  # noqa: S608
            maximum = cursor.fetchone()[0]
            cursor.execute(f"SELECT last_value, is_called FROM {quoted_sequence}")  # noqa: S608
            last_value, is_called = cursor.fetchone()
            cursor.execute(
                "SELECT start_value, increment_by FROM pg_sequences "
                "WHERE schemaname = %s AND sequencename = %s",
                [schema_name.strip('"'), sequence_name.strip('"')],
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"Sequence metadata is missing for {sequence}.")
            start_value, increment_by = row
            next_value = last_value + increment_by if is_called else last_value
            cursor.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = %s::regclass",
                [sequence],
            )
            owner = cursor.fetchone()[0]
            cursor.execute(
                "SELECT has_sequence_privilege(current_user, %s, 'USAGE,SELECT,UPDATE')",
                [sequence],
            )
            privileged = cursor.fetchone()[0]
            if maximum is None:
                if next_value < start_value:
                    raise RuntimeError(
                        f"Empty table {table} would generate {next_value}, "
                        f"below sequence start {start_value}."
                    )
            elif next_value <= maximum:
                raise RuntimeError(
                    f"Sequence {sequence} would generate {next_value} "
                    f"after MAX({column})={maximum}."
                )
            if owner != current_user or not privileged:
                raise RuntimeError(f"Sequence ownership or privileges are invalid for {sequence}.")
            statuses.append(
                {
                    "model": model._meta.label_lower,
                    "table": table,
                    "column": column,
                    "sequence": sequence,
                    "maximum": maximum,
                    "next_value": next_value,
                    "owner": owner,
                }
            )
    return statuses
