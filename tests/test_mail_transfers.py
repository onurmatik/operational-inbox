from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from moto import mock_aws

from inbox.models import (
    Conversation,
    Domain,
    DomainTest,
    DurableJob,
    MailTransfer,
    Message,
    Notification,
    OutboundMessage,
    User,
)
from inbox.services.attachments import AttachmentLockedError, authorized_attachment_url
from inbox.services.mail_transfers import (
    complete_upload,
    download_url,
    maintain_transfers,
    process_transfer,
    start_transfer,
    upload_form,
)
from inbox.services.mbox import decode_entry, encode_entry, entries
from inbox.services.mime import parse_mime
from inbox.services.retention import purge_retention

pytestmark = pytest.mark.django_db


@pytest.fixture
def storage(settings):
    settings.AWS_INGRESS_BUCKET = "mail-transfer-tests"
    settings.AWS_REGION = "us-east-1"
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=settings.AWS_INGRESS_BUCKET)
        yield client


def mail(
    *,
    identifier="<old@example.net>",
    sender="alice@example.net",
    to="support@example.com",
    body="Hello world",
    at="Tue, 04 Nov 2025 09:00:00 +0000",
    parent=None,
    attachment=False,
):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = "Geçmiş mesaj · résumé"
    if identifier:
        message["Message-ID"] = identifier
    if at:
        message["Date"] = at
    if parent:
        message["In-Reply-To"] = parent
    message.set_content(body)
    if attachment:
        message.add_attachment(
            b"PRIVATE ATTACHMENT CONTENT",
            maintype="application",
            subtype="octet-stream",
            filename="locked.bin",
        )
    return message.as_bytes()


def archive(*messages):
    return b"".join(
        encode_entry(message, sender="alice@example.net", at=datetime(2025, 11, 4, tzinfo=UTC))
        for message in messages
    )


def start_import(owner, domain, storage, data):
    transfer = start_transfer(
        domain=domain, owner=owner, kind="IMPORT", filename="history.mbox", size=len(data)
    )
    storage.put_object(Bucket="mail-transfer-tests", Key=transfer.upload_key, Body=data)
    return complete_upload(transfer, client=storage)


def run(transfer, storage):
    for _ in range(100):
        transfer.refresh_from_db()
        if transfer.status in ("COMPLETE", "FAILED", "EXPIRED"):
            return transfer
        process_transfer(transfer.id, client=storage)
    pytest.fail("Transfer did not finish")


def exported_bytes(transfer, storage):
    return b"".join(
        storage.get_object(Bucket="mail-transfer-tests", Key=f["key"])["Body"].read()
        for f in transfer.files
    )


def test_mbox_round_trip_quotes_unicode_and_envelopes():
    raw = mail(body="From a line\n>From another\n>>From third\nTürkçe ✓\n")
    items = list(entries(io.BytesIO(archive(raw, mail(identifier="<two@x.net>")))))
    assert len(items) == 2
    assert parse_mime(decode_entry(items[0].raw)).text_body == parse_mime(raw).text_body.replace(
        "\r\n", "\n"
    )
    assert items[1].offset == items[0].length
    assert list(entries(io.BytesIO(archive(raw).replace(b"\n", b"\r\n"))))
    with pytest.raises(ValidationError):
        list(entries(io.BytesIO(raw)))


def test_import_history_locks_attachments_and_has_no_live_side_effects(owner, domain, storage):
    data = archive(
        mail(attachment=True),
        mail(
            identifier="<sent@example.com>",
            sender="support@example.com",
            to="alice@example.net",
            parent="<old@example.net>",
        ),
    )
    with (
        patch("inbox.services.notifications.create_security_notifications") as notifications,
        patch("inbox.services.outbound.submit_outbound") as send,
    ):
        transfer = run(start_import(owner, domain, storage, data), storage)
    assert transfer.status == "COMPLETE"
    assert transfer.items.filter(status="IMPORTED").count() == 2
    assert Conversation.objects.filter(domain=domain).count() == 1
    inbound = Message.objects.get(domain=domain, direction="INBOUND")
    assert inbound.received_at.year == 2025
    assert inbound.imported_at and inbound.viewed_at
    assert not inbound.is_quarantined and inbound.virus_verdict == "UNKNOWN"
    attachment = inbound.attachments.get()
    assert attachment.scan_status == "UNKNOWN"
    assert attachment.purge_at > timezone.now()
    with pytest.raises(AttachmentLockedError):
        authorized_attachment_url(attachment=attachment, domain=domain, s3_client=storage)
    assert not Notification.objects.exists()
    assert not OutboundMessage.objects.exists()
    assert not DomainTest.objects.exists()
    notifications.assert_not_called()
    send.assert_not_called()


def test_reimport_skips_duplicates_and_preserves_organization(owner, domain, storage):
    data = archive(mail())
    run(start_import(owner, domain, storage, data), storage)
    message = Message.objects.get(domain=domain)
    imported_at = message.imported_at
    conversation = message.conversation
    conversation.starred_at = conversation.archived_at = timezone.now()
    conversation.save()
    duplicate = run(start_import(owner, domain, storage, data), storage)
    assert duplicate.items.get().status == "DUPLICATE"
    assert Message.objects.filter(domain=domain).count() == 1
    message.refresh_from_db()
    conversation.refresh_from_db()
    assert message.imported_at == imported_at
    assert conversation.starred_at and conversation.archived_at
    conflict = run(start_import(owner, domain, storage, archive(mail(body="Different"))), storage)
    assert conflict.items.get().error_code == "message_id_conflict"
    assert message.text_body == "Hello world"


def test_late_parent_and_missing_dates(owner, domain, storage):
    data = archive(
        mail(identifier="<child@x.net>", parent="<parent@x.net>", at=None),
        mail(identifier="<parent@x.net>", at=None),
    )
    transfer = run(start_import(owner, domain, storage, data), storage)
    assert transfer.status == "COMPLETE"
    assert Message.objects.filter(domain=domain).count() == 2
    assert Conversation.objects.filter(domain=domain).count() == 1
    assert Message.objects.first().received_at.year == 2025


def test_invalid_messages_do_not_rollback_valid_messages(owner, domain, storage):
    data = archive(mail()) + b"From bad@example.net Tue Nov 04 00:00:00 2025\nnot an email\n\n"
    transfer = run(start_import(owner, domain, storage, data), storage)
    assert transfer.status == "COMPLETE"
    assert transfer.items.filter(status="FAILED").count() == 1
    assert transfer.items.filter(status="IMPORTED").count() == 1


def test_index_and_import_resume_without_repeating_messages(owner, domain, storage):
    data = archive(*(mail(identifier=f"<{i}@example.net>") for i in range(30)))
    transfer = start_import(owner, domain, storage, data)
    process_transfer(transfer.id, client=storage)
    transfer.refresh_from_db()
    assert transfer.items.count() == 25
    assert transfer.checkpoint["offset"] > 0
    run(transfer, storage)
    assert transfer.items.filter(status="IMPORTED").count() == 30
    assert Message.objects.filter(domain=domain).count() == 30
    process_transfer(transfer.id, client=storage)
    assert Message.objects.filter(domain=domain).count() == 30


def test_import_retention_uses_import_time(owner, domain, storage):
    run(start_import(owner, domain, storage, archive(mail(attachment=True))), storage)
    message = Message.objects.get(domain=domain)
    retention = domain.retention_policy
    retention.raw_message_days = retention.attachment_days = retention.normalized_content_days = 1
    retention.save()
    purge_retention(s3_client=storage, now=timezone.now())
    message.refresh_from_db()
    assert message.text_body and message.raw_s3_key
    purge_retention(s3_client=storage, now=message.imported_at + timedelta(days=2))
    message.refresh_from_db()
    assert message.normalized_purged_at and message.raw_purged_at
    assert message.attachments.get().scan_status == "EXPIRED"


def test_export_never_includes_locked_bytes_even_from_raw_mime(owner, domain, storage):
    run(start_import(owner, domain, storage, archive(mail(attachment=True))), storage)
    transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
    result = exported_bytes(transfer, storage)
    items = list(entries(io.BytesIO(result)))
    parsed = parse_mime(decode_entry(items[0].raw))
    assert parsed.text_body == "Hello world"
    assert not parsed.attachments
    assert b"PRIVATE ATTACHMENT" not in result
    assert transfer.items.get().omitted_attachments == 1
    assert transfer.items.get().reconstructed
    assert download_url(transfer, 0, client=storage)


def test_export_retention_quarantine_snapshot_and_splitting(owner, domain, storage, settings):
    run(
        start_import(
            owner,
            domain,
            storage,
            archive(*(mail(identifier=f"<{i}@example.net>") for i in range(4))),
        ),
        storage,
    )
    messages = list(Message.objects.filter(domain=domain).order_by("rfc_message_id"))
    messages[0].is_quarantined = True
    messages[0].save()
    messages[1].normalized_purged_at = timezone.now()
    messages[1].save()
    transfer = start_transfer(domain=domain, owner=owner, kind="EXPORT")
    # A later message must not enter the snapshot.
    Message.objects.create(
        domain=domain,
        conversation=messages[0].conversation,
        provider_message_id="later",
        direction="INBOUND",
        from_address="later@x.net",
        received_at=timezone.now(),
    )
    settings.MAIL_TRANSFER_MAX_BYTES = 600
    run(transfer, storage)
    assert len(transfer.files) == 2
    assert all(f["size"] <= 600 for f in transfer.files)
    assert transfer.items.count() == 4
    assert transfer.items.filter(status="SKIPPED").count() == 2


def test_upload_sealing_and_size_limits(owner, domain, storage, settings):
    with pytest.raises(ValidationError):
        start_transfer(
            domain=domain, owner=owner, kind="IMPORT", size=settings.MAIL_TRANSFER_MAX_BYTES + 1
        )
    data = archive(mail())
    transfer = start_import(owner, domain, storage, data)
    storage.put_object(Bucket="mail-transfer-tests", Key=transfer.upload_key, Body=b"replacement")
    complete_upload(transfer, client=storage)
    assert (
        storage.get_object(Bucket="mail-transfer-tests", Key=transfer.source_key)["Body"].read()
        == data
    )
    form = upload_form(transfer, client=storage)
    assert form["fields"]["key"] == transfer.upload_key
    assert form["fields"]["Content-Type"] == "application/octet-stream"


def test_disabled_domain_export_and_import_authorization(owner, domain, storage, client):
    domain.status = Domain.Status.DISABLED
    domain.save()
    client.force_login(owner)
    response = client.get(reverse("mail_transfers", args=(domain.id,)))
    assert response.status_code == 200
    assert b"Import requires" in response.content
    assert b"Export emails" in response.content
    with pytest.raises(ValidationError):
        start_transfer(domain=domain, owner=owner, kind="IMPORT", size=10)
    transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
    assert transfer.status == "COMPLETE"


def test_web_tenant_isolation_csrf_status_and_download(owner, domain, storage, client):
    transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
    other = User.objects.create_user(
        email="other@example.net", is_active=True, email_verified_at=timezone.now()
    )
    client.force_login(other)
    for name, args in (
        ("mail_transfers", (domain.id,)),
        ("mail_transfer_status", (domain.id,)),
        ("mail_transfer_download", (domain.id, transfer.id, 0)),
    ):
        assert client.get(reverse(name, args=args)).status_code == 404
    assert client.post(reverse("mail_transfer_export", args=(domain.id,))).status_code == 404
    secured = Client(enforce_csrf_checks=True)
    secured.force_login(owner)
    assert secured.post(reverse("mail_transfer_export", args=(domain.id,))).status_code == 403
    client.force_login(owner)
    response = client.get(reverse("mail_transfer_status", args=(domain.id,)))
    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert "domains/" not in response.content.decode()
    assert not response.json()["active"]


def test_nested_eml_is_a_locked_attachment_not_message_body(owner, domain, storage):
    outer = EmailMessage()
    outer["From"] = "alice@example.net"
    outer["To"] = "support@example.com"
    outer.set_content("Outer body")
    inner = EmailMessage()
    inner["From"] = "secret@example.org"
    inner.set_content("ATTACHED MESSAGE SECRET")
    outer.add_attachment(inner)
    run(start_import(owner, domain, storage, archive(outer.as_bytes())), storage)
    message = Message.objects.get(domain=domain)
    assert "SECRET" not in message.text_body
    assert message.attachments.get().scan_status == "UNKNOWN"


def test_cleanup_and_expired_download(owner, domain, storage):
    run(start_import(owner, domain, storage, archive(mail())), storage)
    transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
    key = transfer.files[0]["key"]
    transfer.expires_at = timezone.now() - timedelta(seconds=1)
    transfer.save()
    with pytest.raises(ValidationError):
        download_url(transfer, 0, client=storage)
    maintain_transfers(client=storage)
    transfer.refresh_from_db()
    assert transfer.status == "EXPIRED"
    assert key not in [
        obj["Key"]
        for obj in storage.list_objects_v2(Bucket="mail-transfer-tests").get("Contents", [])
    ]


def test_only_one_active_transfer_and_import_permission_rechecked(owner, domain, storage):
    transfer = start_import(owner, domain, storage, archive(mail()))
    with pytest.raises(ValidationError):
        start_transfer(domain=domain, owner=owner, kind="EXPORT")
    domain.status = "DISABLED"
    domain.save()
    run(transfer, storage)
    assert transfer.status == "FAILED"
    assert transfer.error_code == "read_only"
    assert not Message.objects.filter(domain=domain).exists()


def test_scheduler_repairs_enqueue_gap(owner, domain, storage):
    transfer = start_transfer(domain=domain, owner=owner, kind="EXPORT")
    DurableJob.objects.filter(kind="mail_transfer").delete()
    assert maintain_transfers(client=storage) == 1
    assert DurableJob.objects.filter(
        kind="mail_transfer", payload__transfer_id=str(transfer.id)
    ).exists()


def test_storage_retry_preserves_committed_messages(owner, domain, storage):
    transfer = start_import(
        owner,
        domain,
        storage,
        archive(mail(attachment=True), mail(identifier="<second@example.net>", attachment=True)),
    )
    process_transfer(transfer.id, client=storage)
    transfer.refresh_from_db()
    generation = transfer.generation
    put_object = storage.put_object
    writes = 0

    def interrupted_put(**kwargs):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise EndpointConnectionError(endpoint_url="https://s3.example.test")
        return put_object(**kwargs)

    with (
        patch.object(storage, "put_object", side_effect=interrupted_put),
        pytest.raises(EndpointConnectionError),
    ):
        process_transfer(transfer.id, client=storage, generation=generation)
    transfer.refresh_from_db()
    assert transfer.generation == generation
    assert transfer.worker_token is None and transfer.leased_until is None
    assert transfer.items.filter(status="IMPORTED").count() == 1
    committed = Message.objects.get(domain=domain)
    imported_at = committed.imported_at
    run(transfer, storage)
    committed.refresh_from_db()
    assert transfer.status == "COMPLETE"
    assert Message.objects.filter(domain=domain).count() == 2
    assert committed.imported_at == imported_at
    assert transfer.items.filter(status="IMPORTED").count() == 2


def test_old_job_cannot_process_or_fail_new_generation(owner, domain, storage):
    from inbox.services.jobs import _surface_job_failure

    transfer = start_import(owner, domain, storage, archive(mail()))
    old_generation = transfer.generation
    process_transfer(transfer.id, client=storage, generation=old_generation)
    transfer.refresh_from_db()
    assert transfer.generation > old_generation
    process_transfer(transfer.id, client=storage, generation=old_generation)
    _surface_job_failure(
        DurableJob(
            kind="mail_transfer",
            payload={"transfer_id": str(transfer.id), "generation": old_generation},
        ),
        terminal=True,
    )
    transfer.refresh_from_db()
    assert transfer.status == "RUNNING"
    assert not Message.objects.filter(domain=domain).exists()
    run(transfer, storage)
    assert transfer.items.get().status == "IMPORTED"


def test_clean_attachment_export_and_missing_raw_reconstruction(owner, domain, storage):
    run(start_import(owner, domain, storage, archive(mail(attachment=True))), storage)
    message = Message.objects.get(domain=domain)
    message.virus_verdict = "PASS"
    message.save()
    attachment = message.attachments.get()
    attachment.scan_status = "CLEAN"
    attachment.save()
    for remove_raw in (False, True):
        if remove_raw:
            storage.delete_object(Bucket="mail-transfer-tests", Key=message.raw_s3_key)
        transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
        item = transfer.items.get()
        result = list(entries(io.BytesIO(exported_bytes(transfer, storage))))
        parsed = parse_mime(decode_entry(result[0].raw))
        assert parsed.attachments[0].content == b"PRIVATE ATTACHMENT CONTENT"
        assert parsed.attachments[0].sha256 == attachment.sha256
        assert parsed.sent_at == message.sent_at
        assert item.reconstructed is remove_raw
        assert item.omitted_attachments == 0


def test_untracked_raw_attachment_cannot_bypass_export_lock(owner, domain, storage):
    run(start_import(owner, domain, storage, archive(mail(attachment=True))), storage)
    message = Message.objects.get(domain=domain)
    message.virus_verdict = "PASS"
    message.save()
    message.attachments.all().delete()
    transfer = run(start_transfer(domain=domain, owner=owner, kind="EXPORT"), storage)
    result = list(entries(io.BytesIO(exported_bytes(transfer, storage))))
    assert not parse_mime(decode_entry(result[0].raw)).attachments
    assert transfer.items.get().reconstructed


def test_web_upload_export_download_and_mutation_isolation(owner, domain, storage):
    client = Client(enforce_csrf_checks=True)
    client.force_login(owner)
    page_url = reverse("mail_transfers", args=(domain.id,))
    assert client.get(page_url).status_code == 200
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    data = archive(mail())
    with patch("inbox.services.mail_transfers.s3_client", return_value=storage):
        response = client.post(
            reverse("mail_transfer_prepare", args=(domain.id,)),
            {"filename": "history.mbox", "size": len(data)},
            **csrf,
        )
        assert response.status_code == 201
        upload = response.json()
        transfer = MailTransfer.objects.get(domain=domain)
        storage.put_object(
            Bucket="mail-transfer-tests", Key=upload["upload"]["fields"]["key"], Body=data
        )
        other = User.objects.create_user(
            email="transfer-other@example.net", is_active=True, email_verified_at=timezone.now()
        )
        other_client = Client()
        other_client.force_login(other)
        for url in (
            reverse("mail_transfer_prepare", args=(domain.id,)),
            upload["complete_url"],
            upload["cancel_url"],
        ):
            assert other_client.post(url).status_code == 404
        assert (
            client.post(upload["complete_url"], HTTP_ACCEPT="application/json", **csrf).status_code
            == 200
        )
        run(transfer, storage)
        assert (
            client.post(reverse("mail_transfer_export", args=(domain.id,)), **csrf).status_code
            == 302
        )
        export = run(MailTransfer.objects.get(domain=domain, kind="EXPORT"), storage)
        download = reverse("mail_transfer_download", args=(domain.id, export.id, 0))
        assert other_client.get(download).status_code == 404
        response = client.get(download)
        assert response.status_code == 302
        assert "response-content-disposition=" in response.url
        response = client.get(reverse("mail_transfer_status", args=(domain.id,)))
        assert not response.json()["active"]
        assert "1 imported" in response.json()["html"]
        assert "1 exported" in response.json()["html"]
        assert "transfers/" not in response.json()["html"].replace("mail-transfers/", "")
        export.expires_at = timezone.now() - timedelta(seconds=1)
        export.save()
        assert client.get(download).status_code == 404
