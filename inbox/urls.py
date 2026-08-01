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
    path(
        "app/organizations/switch/",
        views.organization_switch,
        name="organization_switch",
    ),
    path("app/inbox/", views.inbox_list, name="inbox"),
    path(
        "app/conversations/<uuid:conversation_id>/",
        views.conversation_detail,
        name="conversation_detail",
    ),
    path(
        "app/conversations/<uuid:conversation_id>/status/",
        views.conversation_status,
        name="conversation_status",
    ),
    path(
        "app/conversations/<uuid:conversation_id>/draft/",
        views.draft_generate,
        name="draft_generate",
    ),
    path("app/drafts/<uuid:draft_id>/revise/", views.draft_revise, name="draft_revise"),
    path("app/drafts/<uuid:draft_id>/approve/", views.draft_approve, name="draft_approve"),
    path(
        "app/outbound/<uuid:outbound_id>/resend/",
        views.outbound_resend,
        name="outbound_resend",
    ),
    path("app/domains/", views.domains_list, name="domains"),
    path("app/domains/new/", views.domain_create_view, name="domain_create"),
    path(
        "app/domains/inspect-mx/",
        views.domain_mx_inspect,
        name="domain_mx_inspect",
    ),
    path("app/domains/<uuid:domain_id>/", views.domain_detail, name="domain_detail"),
    path(
        "app/domains/<uuid:domain_id>/test/",
        views.domain_create_test,
        name="domain_create_test",
    ),
    path(
        "app/domains/<uuid:domain_id>/disable/",
        views.domain_disable,
        name="domain_disable",
    ),
    path("app/projects/", views.projects, name="projects"),
    path("app/reports/", views.reports_list, name="reports"),
    path("app/notifications/", views.notifications_list, name="notifications"),
    path("app/settings/schedules/", views.schedules_settings, name="schedules_settings"),
    path("app/settings/api-tokens/", views.api_tokens, name="api_tokens"),
    path(
        "app/settings/api-tokens/<uuid:token_id>/revoke/",
        views.api_token_revoke,
        name="api_token_revoke",
    ),
    path("app/settings/audit/", views.audit_log, name="audit"),
    path(
        "app/attachments/<uuid:attachment_id>/download/",
        views.attachment_download,
        name="attachment_download",
    ),
]
