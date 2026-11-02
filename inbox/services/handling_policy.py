"""Deterministic authorization. Model output never grants tool authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inbox.models import ConversationHandling, Domain, HandlingPolicy, Message
from inbox.services.outbound import get_outbound_control
from inbox.services.tags import normalize_tag

TYPES = (
    "billing",
    "support",
    "sales",
    "partnership",
    "transactional",
    "newsletter",
    "personal",
    "spam",
    "other",
)
RISKS = ("low", "medium", "high", "unknown")
ACTIONS = ("auto", "draft", "review", "archive")


def validate_rules(rules: Any) -> None:
    if not isinstance(rules, list) or len(rules) > 30:
        raise ValidationError("Use at most 30 ordered rules.")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {
            "name",
            "types",
            "risks",
            "tags",
            "action",
            "add_tags",
        }:
            raise ValidationError("Invalid rule fields.")
        if rule.get("action") not in ACTIONS or not isinstance(rule.get("name", ""), str):
            raise ValidationError("Select a supported rule action.")
        if len(rule.get("name", "")) > 80:
            raise ValidationError("Rule names must be 80 characters or fewer.")
        for key, choices in (
            ("types", TYPES),
            ("risks", RISKS),
            ("tags", None),
            ("add_tags", None),
        ):
            values = rule.get(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 20
                or any(not isinstance(v, str) for v in values)
            ):
                raise ValidationError("Invalid rule conditions.")
            if choices and set(values) - set(choices):
                raise ValidationError("Unknown type or risk.")
            if choices is None:
                for value in values:
                    normalize_tag(value)


def account_policy(owner) -> HandlingPolicy:
    policy, _ = HandlingPolicy.objects.get_or_create(
        owner=owner, domain=None, defaults={"enabled_at": owner.date_joined}
    )
    return policy


def effective_policy(domain: Domain) -> dict[str, Any]:
    account = account_policy(domain.owner)
    override = HandlingPolicy.objects.filter(owner=domain.owner, domain=domain).first()
    selected = override if override and override.mode != "inherit" else account
    policies = [p for p in (override, account) if p is not None]
    result = {
        "mode": selected.mode,
        "knowledge": override.knowledge if override else "",
        "rules": [rule for p in policies for rule in p.rules],
        "followup_enabled": selected.followup_enabled,
        "followup_hours": selected.followup_hours,
        "versions": [(str(p.id), p.version) for p in policies],
        "enabled_at": selected.enabled_at.isoformat(),
    }
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


@transaction.atomic
def save_policy(policy: HandlingPolicy) -> HandlingPolicy:
    get_outbound_control(policy.owner, lock=True)
    policy.full_clean()
    old = HandlingPolicy.objects.filter(pk=policy.pk).first()
    policy.version = old.version + 1 if old else 1
    if old and old.mode == "off" and policy.mode != "off":
        policy.enabled_at = timezone.now()
    policy.save()
    states = ConversationHandling.objects.filter(domain__owner=policy.owner, taken_over=False)
    if policy.domain_id:
        states = states.filter(domain_id=policy.domain_id)
    # Revocation is immediate; policy edits never send mail or backfill history.
    from django.db.models import F

    states.exclude(status="done").update(
        generation=F("generation") + 1,
        status="review",
        followup_at=None,
        reason="Settings changed; hand to agent to reassess.",
        updated_at=timezone.now(),
    )
    return policy


def security_block(message: Message) -> str:
    if message.normalized_purged_at:
        return "Message content has expired."
    if message.is_quarantined or message.virus_verdict != Message.Verdict.PASS:
        return "Quarantined content requires review."
    if message.has_auth_failure or message.is_suspicious:
        return "Sender security checks require review."
    if (
        message.reply_to_address
        and message.reply_to_address.casefold() != message.from_address.casefold()
    ):
        return "Reply-To differs from the sender."
    return ""


def no_reply(message: Message) -> bool:
    headers = message.automation_headers
    local = message.from_address.partition("@")[0].casefold()
    return bool(
        (headers.get("auto_submitted") and headers["auto_submitted"] != "no")
        or headers.get("list_id")
        or headers.get("bulk")
        or headers.get("bounce")
        or headers.get("suppress")
        or headers.get("empty_return_path")
        or local in {"mailer-daemon", "postmaster", "no-reply", "noreply", "do-not-reply"}
        or message.from_address.casefold().endswith("@" + message.domain.hostname.casefold())
    )


def resolve_action(
    policy: dict, classification: dict, tags: list[str], *, explicit=False
) -> tuple[str, list[str], str]:
    if classification.get("confidence", 0) < settings.HANDLING_CONFIDENCE:
        return "review", [], "Classification needs review."
    risk = classification.get("risk", "unknown")
    if risk == "unknown":
        return "review", [], "Threat risk requires review."
    action, add_tags, reason = "auto", [], "Default handling"
    matched = False
    normalized = {normalize_tag(t)[1] for t in tags}
    for rule in policy["rules"]:
        if rule.get("types") and classification["type"] not in rule["types"]:
            continue
        if rule.get("risks") and risk not in rule["risks"]:
            continue
        if rule.get("tags") and not normalized.intersection(
            normalize_tag(t)[1] for t in rule["tags"]
        ):
            continue
        matched = True
        action, add_tags, reason = (
            rule["action"],
            rule.get("add_tags", []),
            rule.get("name") or "Matching rule",
        )
        break
    else:
        if classification["type"] in {"newsletter", "spam"}:
            action = "archive"
    mode = policy["mode"]
    if mode == "off" and not explicit:
        return "review", [], "Automation is off."
    if mode == "review":
        action = "review"
    elif (mode == "draft" or risk == "medium") and action == "auto":
        action = "draft"
    if risk == "high" and not (matched and action == "archive"):
        action = "review"
        reason = "Threat risk requires review."
    if risk == "medium" and action == "archive" and not matched:
        action = "review"
    return action, add_tags, reason
