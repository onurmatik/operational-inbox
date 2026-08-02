from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import Any

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def token_digest(raw_token: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), raw_token.encode(), digestmod=hashlib.sha256
    ).hexdigest()


def content_digest(*parts: str) -> str:
    payload = "\x1f".join(parts).encode()
    return hashlib.sha256(payload).hexdigest()


def reject_header_injection(value: str, field_name: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValidationError({field_name: "Header values cannot contain line breaks."})


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields: Any) -> User:
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).casefold()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields: Any) -> User:
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields: Any
    ) -> User:
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("email_verified_at", timezone.now())
        if not extra_fields.get("is_staff") or not extra_fields.get("is_superuser"):
            raise ValueError("A superuser must have is_staff and is_superuser enabled.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(unique=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []
    objects = UserManager()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.__class__.objects.normalize_email(self.email).casefold()
        super().save(*args, **kwargs)

    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    def __str__(self) -> str:
        return self.email


class UUIDTimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class EmailVerificationToken(UUIDTimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="verification_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(cls, user: User, expires_at: Any) -> tuple[EmailVerificationToken, str]:
        raw = secrets.token_urlsafe(32)
        return cls.objects.create(
            user=user, token_hash=token_digest(raw), expires_at=expires_at
        ), raw

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()


class SignupAttempt(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        SIGNUP = "SIGNUP", "Signup"
        VERIFICATION_RESEND = "VERIFICATION_RESEND", "Verification resend"

    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.SIGNUP)
    fingerprint_hash = models.CharField(max_length=64, db_index=True)
    email_hash = models.CharField(max_length=64, db_index=True)
    accepted = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=("kind", "fingerprint_hash", "-created_at")),
            models.Index(fields=("kind", "email_hash", "-created_at")),
        ]


class Domain(UUIDTimeStampedModel):
    class SetupMode(models.TextChoices):
        DIRECT_MX = "DIRECT_MX", "Direct routing to Operational Inbox"
        PROVIDER_FORWARD = "PROVIDER_FORWARD", "Provider catch-all forwarding"

    class Status(models.TextChoices):
        PROVISIONING = "PROVISIONING", "Provisioning"
        PENDING_DNS = "PENDING_DNS", "Pending DNS"
        PENDING_TEST = "PENDING_TEST", "Pending test delivery"
        READY = "READY", "Ready"
        ERROR = "ERROR", "Error"
        DEGRADED = "DEGRADED", "Degraded"
        DISABLED = "DISABLED", "Disabled"

    class OutboundStatus(models.TextChoices):
        DISABLED = "DISABLED", "Not enabled"
        PROVISIONING = "PROVISIONING", "Provisioning"
        PENDING_DNS = "PENDING_DNS", "Pending DNS"
        READY = "READY", "Ready"
        ERROR = "ERROR", "Error"
        DEGRADED = "DEGRADED", "Degraded"

    class SESIdentityOrigin(models.TextChoices):
        MANAGED = "MANAGED", "Created by Operational Inbox"
        ADOPTION_PENDING = "ADOPTION_PENDING", "Existing identity; ownership pending"
        ADOPTED = "ADOPTED", "Existing identity; ownership verified"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="domains"
    )
    hostname = models.CharField(max_length=253)
    timezone = models.CharField(max_length=64, default="UTC")
    setup_mode = models.CharField(max_length=24, choices=SetupMode.choices)
    inbound_setup_generation = models.PositiveBigIntegerField(default=1)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PROVISIONING)
    ownership_verified = models.BooleanField(default=False)
    inbound_ready = models.BooleanField(default=False)
    outbound_ready = models.BooleanField(default=False)
    outbound_status = models.CharField(
        max_length=24,
        choices=OutboundStatus.choices,
        default=OutboundStatus.DISABLED,
    )
    ses_identity_status = models.CharField(max_length=32, blank=True)
    ses_identity_origin = models.CharField(
        max_length=24, choices=SESIdentityOrigin.choices, blank=True
    )
    existing_mx = models.JSONField(default=list, blank=True)
    claim_expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=240, blank=True)
    outbound_error_code = models.CharField(max_length=64, blank=True)
    outbound_error_message = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ("hostname",)
        constraints = [
            models.UniqueConstraint(
                fields=("hostname",),
                condition=~Q(status="DISABLED"),
                name="uniq_active_domain_hostname",
            )
        ]
        indexes = [models.Index(fields=("status", "last_checked_at"))]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.hostname = self.hostname.rstrip(".").casefold()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.hostname

    @property
    def public_error_message(self) -> str:
        if not self.error_code:
            return ""
        return {
            "claim_expired": "This domain setup expired before ownership could be verified.",
            "dns_drift": "A required ownership or routing DNS record no longer matches.",
            "domain_provision_failed": (
                "Operational Inbox could not finish preparing this domain. "
                "Contact support to retry."
            ),
            "domain_provision_retry": (
                "Operational Inbox is still preparing this domain and will retry automatically."
            ),
            "ses_identity_collision": (
                "Operational Inbox found an existing email configuration for this domain."
            ),
            "ses_identity_not_ready": (
                "Operational Inbox can no longer verify this domain for direct receiving."
            ),
        }.get(
            self.error_code,
            "Operational Inbox could not complete this domain setup. Contact support for help.",
        )

    @property
    def public_outbound_error_message(self) -> str:
        if not self.outbound_error_code:
            return ""
        return {
            "outbound_dns_drift": "The DNS records required for sending no longer match.",
            "outbound_identity_not_ready": (
                "Operational Inbox can no longer verify this domain for sending."
            ),
            "outbound_provision_failed": (
                "Operational Inbox could not finish preparing this domain for sending. "
                "Contact support to retry."
            ),
            "outbound_provision_retry": (
                "Operational Inbox is still preparing sending and will retry automatically."
            ),
        }.get(
            self.outbound_error_code,
            "Operational Inbox could not complete sending setup. Contact support for help.",
        )


class DomainScopedModel(UUIDTimeStampedModel):
    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True


class ReportSchedule(DomainScopedModel):
    class Frequency(models.TextChoices):
        HOURLY = "HOURLY", "Hourly"
        DAILY = "DAILY", "Daily"

    domain = models.OneToOneField(
        Domain, on_delete=models.CASCADE, related_name="report_schedule"
    )
    review_frequency = models.CharField(
        max_length=10, choices=Frequency.choices, default=Frequency.HOURLY
    )
    daily_report_time = models.TimeField(default="09:00")
    aging_reminder_hours = models.PositiveSmallIntegerField(default=24)
    is_enabled = models.BooleanField(default=True)
    last_review_at = models.DateTimeField(null=True, blank=True)
    last_daily_report_local_date = models.DateField(null=True, blank=True)


class RetentionPolicy(DomainScopedModel):
    domain = models.OneToOneField(
        Domain, on_delete=models.CASCADE, related_name="retention_policy"
    )
    raw_message_days = models.PositiveSmallIntegerField(default=90)
    attachment_days = models.PositiveSmallIntegerField(default=90)
    normalized_content_days = models.PositiveSmallIntegerField(default=365)
    audit_metadata_days = models.PositiveSmallIntegerField(default=730)
    delivery_metadata_days = models.PositiveSmallIntegerField(default=730)


class DomainDNSRecord(DomainScopedModel):
    class Purpose(models.TextChoices):
        OWNERSHIP = "OWNERSHIP", "Ownership"
        SES_VERIFICATION = "SES_VERIFICATION", "Operational Inbox verification"
        MX = "MX", "Mail exchange"
        DKIM = "DKIM", "DKIM"
        SPF = "SPF", "SPF"
        DMARC = "DMARC", "DMARC"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        VALID = "VALID", "Valid"
        INVALID = "INVALID", "Invalid"
        MISSING = "MISSING", "Missing"

    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name="dns_records")
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    record_type = models.CharField(max_length=10)
    name = models.CharField(max_length=253)
    value = models.TextField()
    priority = models.PositiveSmallIntegerField(null=True, blank=True)
    ttl = models.PositiveIntegerField(default=300)
    is_required = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    observed_values = models.JSONField(default=list, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    error_message = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("domain", "purpose", "record_type", "name"),
                name="uniq_domain_dns_instruction_target",
            )
        ]


class InboundRoute(DomainScopedModel):
    class Kind(models.TextChoices):
        DIRECT_DOMAIN = "DIRECT_DOMAIN", "Direct domain"
        FORWARDING_ALIAS = "FORWARDING_ALIAS", "Forwarding alias"
        TEST = "TEST", "Test delivery"

    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name="inbound_routes")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    local_part = models.CharField(max_length=96)
    address = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=("address", "is_active"))]


class DomainTest(DomainScopedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        RECEIVED = "RECEIVED", "Received"
        EXPIRED = "EXPIRED", "Expired"

    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name="tests")
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    received_message = models.ForeignKey(
        "Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="domain_tests"
    )

class IngressEvent(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        RETRY = "RETRY", "Retry"
        QUARANTINED = "QUARANTINED", "Quarantined"

    sns_message_id = models.CharField(max_length=128, unique=True)
    ses_message_id = models.CharField(max_length=128, db_index=True)
    source_topic_arn = models.CharField(max_length=512)
    source_bucket = models.CharField(max_length=128)
    source_key = models.CharField(max_length=1024)
    payload_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=240, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)


class Conversation(DomainScopedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        WAITING_EXTERNAL = "WAITING_EXTERNAL", "Waiting for external reply"
        RESOLVED = "RESOLVED", "Resolved"
        QUARANTINED = "QUARANTINED", "Quarantined"

    subject = models.CharField(max_length=998, blank=True)
    normalized_subject = models.CharField(max_length=998, blank=True, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.OPEN)
    first_message_at = models.DateTimeField()
    last_message_at = models.DateTimeField(db_index=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    merge_suggestion = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suggested_duplicates",
    )

    class Meta:
        ordering = ("-last_message_at",)
        indexes = [models.Index(fields=("domain", "status", "-last_message_at"))]


class Message(DomainScopedModel):
    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    class Verdict(models.TextChoices):
        PASS = "PASS", "Pass"
        FAIL = "FAIL", "Fail"
        GRAY = "GRAY", "Gray"
        UNKNOWN = "UNKNOWN", "Unknown"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    provider_message_id = models.CharField(max_length=160)
    rfc_message_id = models.CharField(max_length=998, blank=True, db_index=True)
    from_address = models.EmailField(max_length=320)
    reply_to_address = models.EmailField(max_length=320, blank=True)
    subject = models.CharField(max_length=998, blank=True)
    text_body = models.TextField(blank=True)
    html_body = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(db_index=True)
    spam_verdict = models.CharField(max_length=10, choices=Verdict.choices, default=Verdict.UNKNOWN)
    virus_verdict = models.CharField(
        max_length=10, choices=Verdict.choices, default=Verdict.UNKNOWN
    )
    dkim_verdict = models.CharField(max_length=10, choices=Verdict.choices, default=Verdict.UNKNOWN)
    spf_verdict = models.CharField(max_length=10, choices=Verdict.choices, default=Verdict.UNKNOWN)
    dmarc_verdict = models.CharField(
        max_length=10, choices=Verdict.choices, default=Verdict.UNKNOWN
    )
    is_suspicious = models.BooleanField(default=False)
    is_quarantined = models.BooleanField(default=False)
    raw_s3_key = models.CharField(max_length=1024, blank=True)
    raw_sha256 = models.CharField(max_length=64, blank=True)
    raw_purged_at = models.DateTimeField(null=True, blank=True)
    normalized_purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("received_at", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("domain", "provider_message_id"), name="uniq_domain_provider_message"
            )
        ]
        indexes = [
            models.Index(fields=("domain", "-received_at")),
            models.Index(fields=("conversation", "direction", "-received_at")),
        ]

    def clean(self) -> None:
        super().clean()
        if self.conversation_id and (
            self.conversation.domain_id != self.domain_id
        ):
            raise ValidationError("Conversation and message must belong to the same domain.")
        reject_header_injection(self.subject, "subject")


class MessageRecipient(DomainScopedModel):
    class Kind(models.TextChoices):
        ENVELOPE = "ENVELOPE", "Envelope recipient"
        TO = "TO", "To"
        CC = "CC", "Cc"
        BCC = "BCC", "Bcc"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="recipients")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    address = models.EmailField(max_length=320)
    is_routing_recipient = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("message", "kind", "address"), name="uniq_message_recipient"
            )
        ]

    def clean(self) -> None:
        super().clean()
        if self.message_id and self.domain_id != self.message.domain_id:
            raise ValidationError("Message and recipient must belong to the same domain.")


class MessageReference(DomainScopedModel):
    class Kind(models.TextChoices):
        MESSAGE_ID = "MESSAGE_ID", "Message-ID"
        IN_REPLY_TO = "IN_REPLY_TO", "In-Reply-To"
        REFERENCE = "REFERENCE", "Reference"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="references")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    position = models.PositiveSmallIntegerField(default=0)
    value_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("message", "kind", "position", "value_hash"),
                name="uniq_message_reference",
            )
        ]


class Attachment(DomainScopedModel):
    class ScanStatus(models.TextChoices):
        CLEAN = "CLEAN", "Clean"
        QUARANTINED = "QUARANTINED", "Quarantined"
        UNKNOWN = "UNKNOWN", "Unknown"
        EXPIRED = "EXPIRED", "Expired"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    display_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255)
    detected_content_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    s3_key = models.CharField(max_length=1024)
    scan_status = models.CharField(
        max_length=16, choices=ScanStatus.choices, default=ScanStatus.UNKNOWN
    )
    purge_at = models.DateTimeField()
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("domain", "scan_status", "purge_at"))]


class Classification(DomainScopedModel):
    class Source(models.TextChoices):
        AGENT = "AGENT", "Agent"
        OWNER = "OWNER", "Owner override"

    class Category(models.TextChoices):
        ACTIONABLE = "ACTIONABLE", "Actionable"
        INFORMATIONAL = "INFORMATIONAL", "Informational"
        SUSPICIOUS = "SUSPICIOUS", "Suspicious"
        UNCERTAIN = "UNCERTAIN", "Uncertain"

    class Urgency(models.TextChoices):
        LOW = "LOW", "Low"
        NORMAL = "NORMAL", "Normal"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="classifications")
    source = models.CharField(max_length=10, choices=Source.choices)
    category = models.CharField(max_length=20, choices=Category.choices)
    urgency = models.CharField(max_length=10, choices=Urgency.choices, default=Urgency.NORMAL)
    topic = models.CharField(max_length=120, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    summary = models.TextField(blank=True)
    recommended_action = models.TextField(blank=True)
    requires_reply = models.BooleanField(default=False)
    prompt_injection_suspected = models.BooleanField(default=False)
    is_current = models.BooleanField(default=True)
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_by"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("message",),
                condition=Q(is_current=True),
                name="uniq_current_message_classification",
            )
        ]
        indexes = [models.Index(fields=("domain", "category", "urgency", "-created_at"))]


class AgentRun(DomainScopedModel):
    class Kind(models.TextChoices):
        TRIAGE = "TRIAGE", "Triage"
        DRAFT = "DRAFT", "Draft"
        REPORT = "REPORT", "Report"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        REFUSED = "REFUSED", "Refused"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    model_name = models.CharField(max_length=120)
    reasoning_effort = models.CharField(max_length=16)
    prompt_version = models.CharField(max_length=32)
    schema_version = models.CharField(max_length=32)
    schedule_key = models.CharField(max_length=160, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("domain", "kind", "schedule_key"),
                condition=~Q(schedule_key=""),
                name="uniq_agent_schedule_key",
            )
        ]


class DurableJob(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        LEASED = "LEASED", "Leased"
        COMPLETE = "COMPLETE", "Complete"
        RETRY = "RETRY", "Retry"
        FAILED = "FAILED", "Failed"

    domain = models.ForeignKey(
        Domain, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs"
    )
    kind = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    payload = models.JSONField(default=dict)
    due_at = models.DateTimeField(db_index=True)
    leased_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=8)
    last_error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [models.Index(fields=("status", "due_at", "leased_until"))]


class ReplyDraft(DomainScopedModel):
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="reply_drafts"
    )
    context_message = models.ForeignKey(
        Message, on_delete=models.PROTECT, related_name="reply_drafts"
    )
    current_revision = models.ForeignKey(
        "ReplyDraftRevision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_for_drafts",
    )
    is_stale = models.BooleanField(default=False)
    rejected_at = models.DateTimeField(null=True, blank=True)


class ReplyDraftRevision(DomainScopedModel):
    draft = models.ForeignKey(ReplyDraft, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveSmallIntegerField()
    subject = models.CharField(max_length=998)
    body_text = models.TextField()
    content_hash = models.CharField(max_length=64, editable=False)
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="draft_revisions"
    )
    is_agent_generated = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("draft", "number"), name="uniq_draft_revision_number")
        ]
        ordering = ("draft", "number")

    def clean(self) -> None:
        super().clean()
        if self.draft_id and self.domain_id != self.draft.domain_id:
            raise ValidationError("Draft and revision must belong to the same domain.")
        reject_header_injection(self.subject, "subject")

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("Reply draft revisions are immutable.")
        self.content_hash = content_digest(self.subject, self.body_text)
        super().save(*args, **kwargs)


class DraftApproval(DomainScopedModel):
    revision = models.OneToOneField(
        ReplyDraftRevision, on_delete=models.CASCADE, related_name="approval"
    )
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="draft_approvals")
    content_hash = models.CharField(max_length=64)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    invalidated_reason = models.CharField(max_length=120, blank=True)

    def clean(self) -> None:
        super().clean()
        if not self.revision_id:
            return
        draft = self.revision.draft
        errors: dict[str, str] = {}
        if self.domain_id != draft.domain_id:
            errors["domain"] = "Approval and draft must belong to the same domain."
        if draft.current_revision_id != self.revision_id:
            errors["revision"] = "Only the current exact revision can be approved."
        if self.content_hash != self.revision.content_hash:
            errors["content_hash"] = "The approved content hash does not match the revision."
        if self.approved_by_id != draft.domain.owner_id:
            errors["approved_by"] = "Only the domain owner can approve a reply."
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("Draft approvals are immutable.")
        super().save(*args, **kwargs)


class OutboundMessage(DomainScopedModel):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SUBMITTING = "SUBMITTING", "Submitting"
        ACCEPTED = "ACCEPTED", "Accepted"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        UNKNOWN = "UNKNOWN", "Unknown"
        BOUNCED = "BOUNCED", "Bounced"
        COMPLAINED = "COMPLAINED", "Complained"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="outbound_messages"
    )
    revision = models.ForeignKey(
        ReplyDraftRevision, on_delete=models.PROTECT, related_name="outbound_messages"
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="resends"
    )
    attempt_number = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    from_address = models.EmailField(max_length=320)
    to_address = models.EmailField(max_length=320)
    subject = models.CharField(max_length=998)
    body_text = models.TextField()
    content_hash = models.CharField(max_length=64)
    rfc_message_id = models.CharField(max_length=998)
    provider_message_id = models.CharField(max_length=160, blank=True, db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=240, blank=True)

    @property
    def public_error_message(self) -> str:
        if not self.error_code:
            return ""
        if self.error_code == "ses_acceptance_unknown":
            return (
                "Operational Inbox could not confirm whether this message was accepted for "
                "delivery. Automatic retry is disabled."
            )
        if self.error_code == "send_authorization_revoked":
            return self.error_message
        return "Operational Inbox could not send this message."

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("revision", "attempt_number"), name="uniq_revision_send_attempt"
            )
        ]

    def clean(self) -> None:
        super().clean()
        reject_header_injection(self.subject, "subject")
        reject_header_injection(self.from_address, "from_address")
        reject_header_injection(self.to_address, "to_address")
        if self.revision_id and self.content_hash != self.revision.content_hash:
            raise ValidationError(
                {"content_hash": "Outbound content must match its exact revision."}
            )
        if self.content_hash != content_digest(self.subject, self.body_text):
            raise ValidationError(
                {"content_hash": "Outbound subject and body must match the approved content."}
            )
        if self.revision_id:
            approval = getattr(self.revision, "approval", None)
            if (
                approval is None
                or approval.invalidated_at is not None
                or approval.content_hash != self.content_hash
                or self.revision.draft.current_revision_id != self.revision_id
            ):
                raise ValidationError(
                    "Outbound content requires an active exact-revision approval."
                )


class Report(DomainScopedModel):
    class Kind(models.TextChoices):
        HOURLY = "HOURLY", "Hourly review"
        DAILY = "DAILY", "Daily report"

    class Status(models.TextChoices):
        GENERATING = "GENERATING", "Generating"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    class GenerationMode(models.TextChoices):
        AI = "AI", "AI generated"
        DETERMINISTIC = "DETERMINISTIC", "Deterministic fallback"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    schedule_key = models.CharField(max_length=160)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.GENERATING)
    generation_mode = models.CharField(
        max_length=16, choices=GenerationMode.choices, default=GenerationMode.DETERMINISTIC
    )
    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    agent_run = models.ForeignKey(
        AgentRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="reports"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("domain", "kind", "schedule_key"), name="uniq_report_schedule_key"
            )
        ]
        ordering = ("-period_end",)


class ReportItem(DomainScopedModel):
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="items")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="report_items"
    )
    classification = models.ForeignKey(
        Classification,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="report_items",
    )
    rank = models.PositiveSmallIntegerField()
    summary = models.TextField()

    class Meta:
        ordering = ("rank",)
        constraints = [
            models.UniqueConstraint(fields=("report", "rank"), name="uniq_report_item_rank")
        ]


class Notification(DomainScopedModel):
    class Channel(models.TextChoices):
        IN_APP = "IN_APP", "In application"
        EMAIL = "EMAIL", "Email"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        READ = "READ", "Read"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, null=True, blank=True, related_name="notifications"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    kind = models.CharField(max_length=64)
    dedupe_key = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    title = models.CharField(max_length=200)
    body = models.TextField()
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("domain", "channel", "dedupe_key"),
                name="uniq_notification_dedupe",
            )
        ]
        ordering = ("-created_at",)


class DeliveryEvent(DomainScopedModel):
    outbound_message = models.ForeignKey(
        OutboundMessage,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="delivery_events",
    )
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, null=True, blank=True, related_name="delivery_events"
    )
    provider_event_id = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=32)
    provider_message_id = models.CharField(max_length=160, blank=True, db_index=True)
    metadata = models.JSONField(default=dict)
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ("-occurred_at",)


class AuditEventQuerySet(models.QuerySet["AuditEvent"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise ValidationError("Audit events are append-only.")

    def update(self, **kwargs: Any) -> int:
        raise ValidationError("Audit events are append-only.")


class AuditEvent(DomainScopedModel):
    class ActorType(models.TextChoices):
        OWNER = "OWNER", "Owner"
        SYSTEM = "SYSTEM", "System"
        AGENT = "AGENT", "Agent"
        AWS = "AWS", "Operational Inbox"

    actor_type = models.CharField(max_length=10, choices=ActorType.choices)
    actor_id = models.UUIDField(null=True, blank=True)
    event_type = models.CharField(max_length=96)
    object_type = models.CharField(max_length=64)
    object_id = models.UUIDField(null=True, blank=True)
    request_id = models.CharField(max_length=64, db_index=True)
    metadata = models.JSONField(default=dict)

    objects = AuditEventQuerySet.as_manager()
    all_objects = models.Manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("domain", "event_type", "-created_at"))]

    @property
    def public_event_type(self) -> str:
        return {
            "domain.ses_identity_adoption_pending": "domain.email_configuration_review_started",
            "domain.ses_identity_reinitialized": "domain.email_configuration_refreshed",
            "domain.ses_identity_adopted": "domain.email_configuration_connected",
        }.get(self.event_type, self.event_type)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Audit events are append-only.")


class APIToken(DomainScopedModel):
    class Scope(models.TextChoices):
        READ = "read", "Read"
        WRITE = "write", "Write"
        APPROVE_SEND = "approve_send", "Approve and send"

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=12, db_index=True)
    token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def issue(
        cls,
        *,
        domain: Domain,
        owner: User,
        name: str,
        scopes: list[str],
        expires_at: Any = None,
    ) -> tuple[APIToken, str]:
        allowed = {choice for choice, _ in cls.Scope.choices}
        if not scopes or not set(scopes).issubset(allowed):
            raise ValidationError({"scopes": "Select one or more valid token scopes."})
        if owner.id != domain.owner_id:
            raise ValidationError({"owner": "Only the domain owner can create API tokens."})
        raw = f"oi_{secrets.token_urlsafe(36)}"
        token = cls.objects.create(
            domain=domain,
            owner=owner,
            name=name,
            prefix=raw[:10],
            token_hash=token_digest(raw),
            scopes=sorted(set(scopes)),
            expires_at=expires_at,
        )
        return token, raw

    def matches(self, raw_token: str) -> bool:
        return hmac.compare_digest(self.token_hash, token_digest(raw_token))

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > timezone.now()
        )
