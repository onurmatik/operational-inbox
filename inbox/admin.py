from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from inbox import models


@admin.register(models.User)
class UserAdmin(BaseUserAdmin):
    ordering = ("email",)
    list_display = ("email", "email_verified_at", "is_staff", "is_active")
    search_fields = ("email",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Verification", {"fields": ("email_verified_at",)}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


class DomainScopedAdmin(admin.ModelAdmin):
    readonly_fields = ("id", "created_at", "updated_at")

    def get_list_filter(self, request):
        if any(field.name == "domain" for field in self.model._meta.fields):
            return ("domain",)
        return ()


for model in (
    models.ReportSchedule,
    models.RetentionPolicy,
    models.Domain,
    models.DomainDNSRecord,
    models.InboundRoute,
    models.DomainTest,
    models.Conversation,
    models.Message,
    models.MessageRecipient,
    models.MessageReference,
    models.Attachment,
    models.Classification,
    models.AgentRun,
    models.DurableJob,
    models.ReplyDraft,
    models.ReplyDraftRevision,
    models.DraftApproval,
    models.OutboundMessage,
    models.Report,
    models.ReportItem,
    models.Notification,
    models.DeliveryEvent,
    models.APIToken,
):
    admin.site.register(model, DomainScopedAdmin)


@admin.register(models.BillingProfile)
class BillingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "subscription_status",
        "stripe_customer_id",
        "stripe_subscription_id",
        "updated_at",
    )
    search_fields = ("user__email", "stripe_customer_id", "stripe_subscription_id")
    readonly_fields = [field.name for field in models.BillingProfile._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("stripe_event_id", "event_type", "stripe_created", "created_at")
    search_fields = ("stripe_event_id", "event_type")
    readonly_fields = [field.name for field in models.StripeWebhookEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.IngressEvent)
class IngressEventAdmin(admin.ModelAdmin):
    list_display = ("ses_message_id", "status", "attempts", "created_at")
    list_filter = ("status",)
    search_fields = ("ses_message_id", "sns_message_id", "error_code")
    readonly_fields = [field.name for field in models.IngressEvent._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(models.AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "actor_type", "object_type", "request_id", "created_at")
    list_filter = ("domain", "actor_type", "event_type")
    readonly_fields = [field.name for field in models.AuditEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
