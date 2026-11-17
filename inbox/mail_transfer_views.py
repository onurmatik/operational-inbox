"""Session-only web endpoints. Transfers are intentionally absent from API/MCP."""

import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from inbox.models import Domain, MailTransfer
from inbox.services.mail_transfers import (
    ACTIVE,
    can_import,
    complete_upload,
    download_url,
    fail_transfer,
    start_transfer,
    transfer_summary,
    upload_form,
)
from inbox.views import verified_required

logger = logging.getLogger(__name__)


def _domain(request, domain_id):
    return get_object_or_404(
        Domain.objects.select_related("owner"), id=domain_id, owner=request.user
    )


def _transfer(request, domain_id, transfer_id):
    domain = _domain(request, domain_id)
    return get_object_or_404(
        MailTransfer.objects.select_related("domain"), id=transfer_id, domain=domain
    )


def _results(domain):
    result = []
    for transfer in MailTransfer.objects.filter(domain=domain)[:10]:
        summary = transfer_summary(transfer)
        summary["transfer"] = transfer
        summary["issues"] = list(
            transfer.items.exclude(error_code="").values("position", "error_code")[:100]
        )
        summary["issue_count"] = transfer.items.exclude(error_code="").count()
        summary["downloads"] = []
        if (
            transfer.kind == "EXPORT"
            and transfer.status == "COMPLETE"
            and transfer.expires_at > timezone.now()
        ):
            summary["downloads"] = [
                {
                    "part": index + 1,
                    "size": file["size"],
                    "url": reverse("mail_transfer_download", args=(domain.id, transfer.id, index)),
                }
                for index, file in enumerate(transfer.files)
            ]
        result.append(summary)
    return result


def _error(exc, status=400):
    if isinstance(exc, ValidationError):
        return JsonResponse({"error": exc.messages[0]}, status=status)
    logger.warning("Mail transfer storage unavailable", extra={"error_type": type(exc).__name__})
    return JsonResponse({"error": "Storage is temporarily unavailable. Try again."}, status=503)


@verified_required
@require_GET
@never_cache
def page(request, domain_id):
    domain = _domain(request, domain_id)
    return render(
        request,
        "inbox/mail_transfers.html",
        {
            "active_nav": "domains",
            "domain": domain,
            "results": _results(domain),
            "can_import": can_import(domain, request.user),
            "busy": MailTransfer.objects.filter(domain=domain, status__in=ACTIVE).exists(),
            "storage_ready": bool(settings.AWS_INGRESS_BUCKET),
            "max_bytes": settings.MAIL_TRANSFER_MAX_BYTES,
        },
    )


@verified_required
@require_POST
def prepare_import(request, domain_id):
    domain = _domain(request, domain_id)
    transfer = None
    try:
        if not settings.AWS_INGRESS_BUCKET:
            raise ValidationError("Mail transfer storage is not configured.")
        try:
            size = int(request.POST.get("size", "0"))
        except (ValueError, TypeError) as exc:
            raise ValidationError("Choose an MBOX file of up to 250 MB.") from exc
        transfer = start_transfer(
            domain=domain,
            owner=request.user,
            kind="IMPORT",
            filename=request.POST.get("filename", ""),
            size=size,
        )
        form = upload_form(transfer)
    except (ValidationError, BotoCoreError, ClientError) as exc:
        if transfer:
            fail_transfer(transfer.id, "upload_unavailable")
        return _error(exc)
    return JsonResponse(
        {
            "upload": form,
            "complete_url": reverse("mail_transfer_complete", args=(domain.id, transfer.id)),
            "cancel_url": reverse("mail_transfer_cancel", args=(domain.id, transfer.id)),
            "page_url": reverse("mail_transfers", args=(domain.id,)),
        },
        status=201,
    )


@verified_required
@require_POST
def complete(request, domain_id, transfer_id):
    transfer = _transfer(request, domain_id, transfer_id)
    try:
        transfer = complete_upload(transfer)
    except (ValidationError, BotoCoreError, ClientError) as exc:
        if "application/json" in request.headers.get("Accept", ""):
            return _error(exc)
        messages.error(
            request,
            exc.messages[0]
            if isinstance(exc, ValidationError)
            else "Upload is unavailable. Upload the file again.",
        )
        return redirect("mail_transfers", domain_id=domain_id)
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(transfer_summary(transfer))
    return redirect("mail_transfers", domain_id=domain_id)


@verified_required
@require_POST
def cancel(request, domain_id, transfer_id):
    transfer = _transfer(request, domain_id, transfer_id)
    if transfer.status == "UPLOADING":
        fail_transfer(transfer.id, "upload_cancelled", expected_status="UPLOADING")
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"ok": True})
    return redirect("mail_transfers", domain_id=domain_id)


@verified_required
@require_POST
def start_export(request, domain_id):
    domain = _domain(request, domain_id)
    try:
        if not settings.AWS_INGRESS_BUCKET:
            raise ValidationError("Mail transfer storage is not configured.")
        start_transfer(domain=domain, owner=request.user, kind="EXPORT")
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    return redirect("mail_transfers", domain_id=domain_id)


@verified_required
@require_GET
@never_cache
def status(request, domain_id):
    domain = _domain(request, domain_id)
    results = _results(domain)
    return JsonResponse(
        {
            "html": render_to_string(
                "inbox/partials/mail_transfer_results.html",
                {"results": results, "domain": domain},
                request=request,
            ),
            "active": any(result["active"] for result in results),
        }
    )


@verified_required
@require_GET
@never_cache
def download(request, domain_id, transfer_id, part):
    transfer = _transfer(request, domain_id, transfer_id)
    try:
        url = download_url(transfer, part)
    except ValidationError:
        raise Http404 from None
    except (BotoCoreError, ClientError) as exc:
        return _error(exc)
    return redirect(url)
