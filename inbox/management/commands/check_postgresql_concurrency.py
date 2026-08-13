from __future__ import annotations

import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, close_old_connections, connection
from django.db.models import F, Q
from django.utils import timezone

from inbox.models import APIToken, Domain, DurableJob, User


def _run_parallel(function):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(function) for _ in range(2)]
        return [future.result(timeout=20) for future in futures]


class Command(BaseCommand):
    help = "Run Operational Inbox's PostgreSQL locking and single-winner checks."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("The configured database is not PostgreSQL.")
        marker = secrets.token_hex(10)
        users = [
            User.objects.create_user(email=f"pg-check-{marker}-{index}@example.invalid")
            for index in range(2)
        ]
        try:
            self._check_domain_claim(users, marker)
            self._check_api_token_issue(users[0])
            self._check_job_lease(marker)
        finally:
            User.objects.filter(id__in=[user.id for user in users]).delete()
            DurableJob.objects.filter(idempotency_key=f"pg-check:{marker}").delete()
        self.stdout.write(self.style.SUCCESS("PostgreSQL concurrency checks passed."))

    def _check_domain_claim(self, users, marker):
        barrier = Barrier(2)

        def claim():
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                Domain.objects.create(
                    owner=users[0] if connection.alias == "default" else users[1],
                    hostname=f"pg-check-{marker}.example.invalid",
                    setup_mode=Domain.SetupMode.DIRECT_MX,
                    claim_expires_at=timezone.now(),
                )
            except IntegrityError:
                return "conflict"
            finally:
                close_old_connections()
            return "created"

        results = sorted(_run_parallel(claim))
        if results != ["conflict", "created"]:
            raise CommandError(f"Domain claim race did not have one winner: {results}")

    def _check_api_token_issue(self, user):
        barrier = Barrier(2)

        def issue():
            close_old_connections()
            barrier.wait(timeout=10)
            APIToken.issue(owner=User.objects.get(id=user.id))
            close_old_connections()
            return "issued"

        if _run_parallel(issue) != ["issued", "issued"]:
            raise CommandError("API token serialization did not complete twice.")
        if APIToken.objects.filter(owner=user, revoked_at__isnull=True).count() != 1:
            raise CommandError("API token serialization left more than one active token.")

    def _check_job_lease(self, marker):
        job = DurableJob.objects.create(
            kind="postgresql_concurrency_check",
            idempotency_key=f"pg-check:{marker}",
            due_at=timezone.now(),
        )
        barrier = Barrier(2)

        def lease():
            close_old_connections()
            barrier.wait(timeout=10)
            updated = (
                DurableJob.objects.filter(id=job.id, status=DurableJob.Status.PENDING)
                .filter(Q(leased_until__isnull=True) | Q(leased_until__lte=timezone.now()))
                .update(
                    status=DurableJob.Status.LEASED,
                    leased_until=timezone.now(),
                    attempts=F("attempts") + 1,
                )
            )
            close_old_connections()
            return updated

        if sorted(_run_parallel(lease)) != [0, 1]:
            raise CommandError("DurableJob lease race did not have one winner.")
