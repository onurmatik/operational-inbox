from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inbox.handling_forms import HandlingPolicyForm, HandlingRuleFormSet
from inbox.models import AuditEvent, Conversation, Domain, HandlingPolicy
from inbox.services.handling import set_handling
from inbox.services.handling_policy import account_policy, save_policy
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
        set_handling(conversation, owner=request.user, action=request.POST.get("action", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("conversation_detail", conversation_id=conversation.id)
