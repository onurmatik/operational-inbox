from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from inbox.models import AgentRun, Classification, Domain, Message, Report, ReportItem
from inbox.services.ai import AIProcessingError, generate_report_output


def domain_zone(domain: Domain) -> ZoneInfo:
    try:
        return ZoneInfo(domain.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def schedule_key(domain: Domain, kind: str, now: datetime) -> str:
    local = now.astimezone(domain_zone(domain))
    if kind == Report.Kind.HOURLY:
        return f"{local:%Y-%m-%dT%H}:00:{local.utcoffset()}"
    return f"{local:%Y-%m-%d}:daily"


def report_period(kind: str, now: datetime) -> tuple[datetime, datetime]:
    if kind == Report.Kind.HOURLY:
        end = now.replace(minute=0, second=0, microsecond=0)
        return end - timedelta(hours=1), end
    end = now
    return end - timedelta(days=1), end


@dataclass(frozen=True)
class ReportCandidate:
    message: Message
    classification: Classification | None

    @property
    def category(self) -> str:
        if self.classification is not None:
            return self.classification.category
        if self.message.is_suspicious or self.message.is_quarantined:
            return Classification.Category.SUSPICIOUS
        return Classification.Category.UNCERTAIN

    @property
    def urgency(self) -> str:
        if self.classification is not None:
            return self.classification.urgency
        if self.message.is_quarantined:
            return Classification.Urgency.HIGH
        return Classification.Urgency.NORMAL

    @property
    def summary(self) -> str:
        if self.classification is not None and self.classification.summary:
            return self.classification.summary
        if self.message.is_quarantined:
            return "Quarantined message; content is locked pending owner review."
        if self.message.is_suspicious:
            return "Suspicious message; authentication or spam verdict requires owner review."
        return "Unclassified message; manual review is required."


def _report_candidates(
    domain: Domain, start: datetime, end: datetime
) -> list[ReportCandidate]:
    aging_cutoff = end - timedelta(hours=domain.report_schedule.aging_reminder_hours)
    messages = list(
        Message.objects.filter(
            domain=domain,
            direction=Message.Direction.INBOUND,
            normalized_purged_at__isnull=True,
        )
        .filter(
            Q(received_at__gte=start, received_at__lt=end)
            | Q(
                conversation__status__in=["OPEN", "WAITING_EXTERNAL"],
                conversation__last_message_at__lte=aging_cutoff,
            )
        )
        .select_related("conversation", "domain")
        .prefetch_related(
            Prefetch(
                "classifications",
                queryset=Classification.objects.filter(is_current=True),
            )
        )
        .order_by("-received_at")
    )
    candidates = []
    for message in messages:
        current_classifications = list(message.classifications.all())
        candidates.append(
            ReportCandidate(
                message=message,
                classification=current_classifications[0] if current_classifications else None,
            )
        )
    urgency_rank = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
    return sorted(
        candidates,
        key=lambda item: (
            urgency_rank.get(item.urgency, 4),
            -item.message.received_at.timestamp(),
        ),
    )


def _fallback_content(candidates: list[ReportCandidate]) -> tuple[str, str]:
    actionable = [
        item for item in candidates if item.category == Classification.Category.ACTIONABLE
    ]
    suspicious = [
        item for item in candidates if item.category == Classification.Category.SUSPICIOUS
    ]
    title = "Operational inbox review"
    lines = [
        "Deterministic fallback report (AI was unavailable).",
        f"Messages reviewed: {len(candidates)}.",
        f"Actionable: {len(actionable)}. Suspicious: {len(suspicious)}.",
    ]
    for item in candidates[:25]:
        lines.append(
            f"- [{item.urgency}] {item.message.subject or '(no subject)'} — {item.summary}"
        )
    return title, "\n".join(lines)


def generate_report(
    *, domain: Domain, kind: str, now: datetime | None = None, client=None
) -> Report:
    now = now or timezone.now()
    key = schedule_key(domain, kind, now)
    existing = Report.objects.filter(domain=domain, kind=kind, schedule_key=key).first()
    if existing:
        return existing
    start, end = report_period(kind, now)
    candidates = _report_candidates(domain, start, end)
    title, content = _fallback_content(candidates)
    mode = Report.GenerationMode.DETERMINISTIC
    ai_items: dict[str, tuple[int, str]] = {}
    agent_run = None
    if settings.OPENAI_API_KEY or client is not None:
        input_text = (
            "BEGIN UNTRUSTED CLASSIFICATION DATA\n"
            + "\n".join(
                f"conversation_id={item.message.conversation_id}; urgency={item.urgency}; "
                f"category={item.category}; subject={item.message.subject[:500]!r}; "
                f"summary={item.summary[:1000]!r}"
                for item in candidates[:100]
            )
            + "\nEND UNTRUSTED CLASSIFICATION DATA"
        )
        try:
            output, agent_run = generate_report_output(
                domain=domain,
                schedule_key=key,
                input_text=input_text,
                client=client,
            )
        except AIProcessingError:
            agent_run = AgentRun.objects.filter(
                domain=domain,
                kind=AgentRun.Kind.REPORT,
                schedule_key=key,
            ).first()
        else:
            title = output.title
            content = output.overview
            mode = Report.GenerationMode.AI
            ai_items = {
                item.conversation_id: (item.priority, item.summary) for item in output.items
            }
    with transaction.atomic():
        report, created = Report.objects.get_or_create(
            domain=domain,
            kind=kind,
            schedule_key=key,
            defaults={
                "period_start": start,
                "period_end": end,
                "status": Report.Status.READY,
                "generation_mode": mode,
                "title": title,
                "content": content,
                "agent_run": agent_run,
            },
        )
        if not created:
            return report
        rank = 0
        seen: set[object] = set()
        for candidate in candidates:
            conversation = candidate.message.conversation
            if conversation.id in seen:
                continue
            seen.add(conversation.id)
            rank += 1
            ai_item = ai_items.get(str(conversation.id))
            ReportItem.objects.create(
                domain=domain,
                report=report,
                conversation=conversation,
                classification=candidate.classification,
                rank=rank,
                summary=ai_item[1] if ai_item else candidate.summary,
            )
    return report


def daily_report_due(domain: Domain, now: datetime) -> bool:
    schedule = domain.report_schedule
    local = now.astimezone(domain_zone(domain))
    if not schedule.is_enabled or schedule.last_daily_report_local_date == local.date():
        return False
    scheduled = datetime.combine(local.date(), schedule.daily_report_time, tzinfo=local.tzinfo)
    return local >= scheduled
