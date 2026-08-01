from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import boto3
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage


class SESEmailBackend(BaseEmailBackend):
    def __init__(self, *args: Any, ses_client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client = ses_client or boto3.client("ses", region_name=settings.AWS_REGION)

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        sent = 0
        for email_message in email_messages:
            if not email_message.recipients():
                continue
            try:
                self.client.send_raw_email(
                    Source=email_message.from_email,
                    Destinations=email_message.recipients(),
                    RawMessage={"Data": email_message.message().as_bytes()},
                )
            except Exception:
                if not self.fail_silently:
                    raise
            else:
                sent += 1
        return sent
