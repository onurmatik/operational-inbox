from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inbox.handling_forms import HandlingPolicyForm, HandlingRuleFormSet, ReplyBriefForm
from inbox.models import AuditEvent, Conversation, Domain, HandlingPolicy
from inbox.services.entitlements import can_manage_domain
from inbox.services.handling import set_handling
from inbox.services.handling_model import ModelUnavailable
from inbox.services.handling_policy import account_policy, save_policy
from inbox.services.reply_briefs import suggest_goals
from inbox.services.tenancy import get_owned_domain


@login_required
def agent_settings(request):
    domains = request.user.domains.exclude(status=Domain.Status.DISABLED).order_by("hostname")
    domain = (
        get_owned_domain(request.user, request.GET["domain"]) if request.GET.get("domain") else None
    )
    policy = (
        (
            HandlingPolicy.objects.filter(owner=request.user, domain=domain).first()
            or HandlingPolicy(owner=request.user, domain=domain, mode="inherit")
        )
        if domain
        else account_policy(request.user)
    )
    data = request.POST if request.method == "POST" else None
    form = HandlingPolicyForm(data, instance=policy)
    initial = [
        {
            **rule,
            "tags": ", ".join(rule.get("tags", [])),
            "add_tags": ", ".join(rule.get("add_tags", [])),
        }
        for rule in policy.rules
    ]
    rules = HandlingRuleFormSet(data, initial=initial, prefix="rules")
    if request.method == "POST" and form.is_valid() and rules.is_valid():
        policy = form.save(commit=False)
        policy.rules = [
            {
                key: f.cleaned_data[key]
                for key in ("name", "types", "tags", "risks", "action", "add_tags")
            }
            for f in rules.ordered_forms
        ]
        try:
            save_policy(policy)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            # Policy content stays out of audit logs; each affected domain gets metadata only.
            for item in [domain] if domain else domains:
                AuditEvent.objects.create(
                    domain=item,
                    actor_type="OWNER",
                    actor_id=request.user.id,
                    event_type="agent.policy_updated",
                    object_type="HandlingPolicy",
                    object_id=policy.id,
                    request_id=f"policy:{policy.id}:{policy.version}",
                    metadata={"version": policy.version, "mode": policy.mode},
                )
            messages.success(request, "Agent settings saved.")
            return redirect(request.get_full_path())
    return render(
        request,
        "inbox/settings_agent.html",
        {
            "form": form,
            "rules": rules,
            "domain": domain,
            "domains": domains,
            "active_nav": "agent_settings",
            "page_title": "Agent",
        },
    )


@login_required
@require_POST
def conversation_handling(request, conversation_id):
    conversation = get_object_or_404(
        Conversation.objects.select_related("domain__owner"),
        id=conversation_id,
        domain__owner=request.user,
    )
    try:
        if request.POST.get("action") == "handoff":
            form = ReplyBriefForm(request.POST)
            if not form.is_valid():
                raise ValidationError(
                    "Choose a reply goal or add instructions, then select delivery."
                )
            set_handling(
                conversation,
                owner=request.user,
                action="handoff",
                reply_brief={key: form.cleaned_data[key] for key in ("instructions", "delivery")},
                context_token=form.cleaned_data["context_token"],
            )
            messages.success(request, "Reply request queued. Agent rules still apply.")
        else:
            set_handling(conversation, owner=request.user, action=request.POST.get("action", ""))
    except ValidationError as exc:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"message": " ".join(exc.messages), "stale": getattr(exc, "code", "") == "stale"},
                status=400,
            )
        messages.error(request, " ".join(exc.messages))
    else:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"queued": True})
    return redirect("conversation_detail", conversation_id=conversation.id)


@login_required
@require_POST
def conversation_reply_goals(request, conversation_id):
    conversation = get_object_or_404(
        Conversation.objects.select_related("domain__owner"),
        id=conversation_id,
        domain__owner=request.user,
    )
    if not can_manage_domain(request.user, conversation.domain):
        return JsonResponse({"message": "This conversation is read-only."}, status=403)
    try:
        goals = suggest_goals(conversation, request.POST.get("context_token", ""))
    except ValidationError as exc:
        stale = getattr(exc, "code", "") == "stale"
        return JsonResponse(
            {"message": " ".join(exc.messages), "stale": stale}, status=409 if stale else 400
        )
    except ModelUnavailable:
        return JsonResponse(
            {"goals": [], "message": "Suggestions unavailable. Add your instructions."}
        )
    return JsonResponse({"goals": goals})
