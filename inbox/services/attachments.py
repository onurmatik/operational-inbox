from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from inbox.models import Attachment, Organization


class AttachmentGoneError(RuntimeError):
    pass


class AttachmentLockedError(PermissionDenied):
    pass


@dataclass(frozen=True)
class AuthorizedAttachmentURL:
    url: str
    expires_in: int = 300


def authorized_attachment_url(
    *, attachment: Attachment, organization: Organization, s3_client: Any | None = None
) -> AuthorizedAttachmentURL:
    if attachment.organization_id != organization.id:
        raise PermissionDenied
    if attachment.purged_at or attachment.purge_at <= timezone.now() or not attachment.s3_key:
        raise AttachmentGoneError("This attachment has expired under the retention policy.")
    if attachment.scan_status != Attachment.ScanStatus.CLEAN:
        raise AttachmentLockedError("Quarantined or unscanned attachments cannot be downloaded.")
    s3 = s3_client or boto3.client("s3", region_name=settings.AWS_REGION)
    safe_name = attachment.display_name.replace('"', "").replace("\r", "").replace("\n", "")
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.AWS_INGRESS_BUCKET,
            "Key": attachment.s3_key,
            "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
        },
        ExpiresIn=300,
    )
    return AuthorizedAttachmentURL(url=url)
