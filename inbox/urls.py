from django.urls import path

from inbox import views

urlpatterns = [
    path("", views.home, name="home"),
    path("start/", views.start_onboarding, name="start_onboarding"),
    path("signup/", views.signup, name="signup"),
    path("verify/sent/", views.verification_sent, name="verification_sent"),
    path("verify/resend/", views.verification_resend, name="verification_resend"),
    path("verify/<str:token>/", views.verify_email, name="verify_email"),
    path("app/", views.dashboard, name="dashboard"),
    path("app/agents/", views.agents, name="agents"),
    path(
        "app/domains/switch/",
        views.domain_switch,
        name="domain_switch",
    ),
    path("app/inbox/", views.inbox_list, name="inbox"),
    path("app/outbox/", views.outbox, name="outbox"),
    path("app/outbox/control/", views.outbox_control, name="outbox_control"),
    path(
        "app/conversations/<uuid:conversation_id>/",
        views.conversation_detail,
        name="conversation_detail",
    ),
    path(
        "app/conversations/<uuid:conversation_id>/action/",
        views.conversation_action,
        name="conversation_action",
    ),
    path(
        "app/conversations/<uuid:conversation_id>/tags/",
        views.conversation_tag,
        name="conversation_tag",
    ),
    path("app/drafts/<uuid:draft_id>/revise/", views.draft_revise, name="draft_revise"),
    path("app/drafts/<uuid:draft_id>/approve/", views.draft_approve, name="draft_approve"),
    path(
        "app/outbound/<uuid:outbound_id>/resend/",
        views.outbound_resend,
        name="outbound_resend",
    ),
    path("app/domains/", views.domains_list, name="domains"),
    path(
        "app/domains/<uuid:domain_id>/keep-on-free/",
        views.domain_keep_on_free,
        name="domain_keep_on_free",
    ),
    path("app/domains/new/", views.domain_create_view, name="domain_create"),
    path(
        "app/domains/inspect-mx/",
        views.domain_mx_inspect,
        name="domain_mx_inspect",
    ),
    path("app/domains/<uuid:domain_id>/", views.domain_detail, name="domain_detail"),
    path(
        "app/domains/<uuid:domain_id>/retry/",
        views.domain_retry_provisioning,
        name="domain_retry_provisioning",
    ),
    path(
        "app/domains/<uuid:domain_id>/routing/direct/",
        views.domain_switch_to_direct,
        name="domain_switch_to_direct",
    ),
    path(
        "app/domains/<uuid:domain_id>/routing/transition/",
        views.domain_routing_transition_start,
        name="domain_routing_transition_start",
    ),
    path(
        "app/domains/<uuid:domain_id>/routing/transition/cancel/",
        views.domain_routing_transition_cancel,
        name="domain_routing_transition_cancel",
    ),
    path(
        "app/domains/<uuid:domain_id>/test/",
        views.domain_create_test,
        name="domain_create_test",
    ),
    path(
        "app/domains/<uuid:domain_id>/outbound/enable/",
        views.domain_enable_outbound,
        name="domain_enable_outbound",
    ),
    path(
        "app/domains/<uuid:domain_id>/disable/",
        views.domain_disable,
        name="domain_disable",
    ),
    path("app/settings/retention/", views.retention_settings, name="retention_settings"),
    path("app/settings/api-tokens/", views.api_tokens, name="api_tokens"),
    path(
        "app/settings/api-tokens/<uuid:token_id>/revoke/",
        views.api_token_revoke,
        name="api_token_revoke",
    ),
    path("app/settings/audit/", views.audit_log, name="audit"),
    path("app/billing/", views.billing, name="billing"),
    path("app/billing/checkout/", views.billing_checkout, name="billing_checkout"),
    path("app/billing/portal/", views.billing_portal, name="billing_portal"),
    path("webhooks/stripe/", views.stripe_webhook, name="stripe_webhook"),
    path(
        "app/attachments/<uuid:attachment_id>/download/",
        views.attachment_download,
        name="attachment_download",
    ),
]
