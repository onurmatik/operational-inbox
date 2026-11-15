from __future__ import annotations

from django import forms

from inbox.models import DigestSettings, Domain, RetentionPolicy, User
from inbox.services.domains import normalize_hostname


class StyledFormMixin:
    def _style_fields(self) -> None:
        for field in self.fields.values():
            if isinstance(
                field.widget,
                (
                    forms.CheckboxInput,
                    forms.CheckboxSelectMultiple,
                    forms.RadioSelect,
                ),
            ):
                continue
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"oi-input {existing}".strip()


class StartOnboardingForm(StyledFormMixin, forms.Form):
    hostname = forms.CharField(max_length=253)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["hostname"].widget.attrs.update(
            placeholder="yourdomain.com",
            autocomplete="url",
            inputmode="url",
            spellcheck="false",
        )

    def clean_hostname(self) -> str:
        return normalize_hostname(self.cleaned_data["hostname"])


class SignupForm(StyledFormMixin, forms.Form):
    email = forms.EmailField(max_length=254)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["email"].widget.attrs.update(
            autocomplete="email",
            placeholder="you@company.com",
        )

    def clean_email(self) -> str:
        return User.objects.normalize_email(self.cleaned_data["email"]).casefold()


class VerificationResendForm(StyledFormMixin, forms.Form):
    email = forms.EmailField(max_length=254)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["email"].widget.attrs.update(autocomplete="email")

    def clean_email(self) -> str:
        return User.objects.normalize_email(self.cleaned_data["email"]).casefold()


class DomainForm(StyledFormMixin, forms.Form):
    hostname = forms.CharField(max_length=253)
    goal = forms.ChoiceField(
        choices=[(Domain.Goal.RECEIVE, "Receive email"), (Domain.Goal.BOTH, "Receive & reply")],
        widget=forms.RadioSelect,
        required=False,
    )
    setup_mode = forms.ChoiceField(
        choices=Domain.SetupMode.choices, widget=forms.RadioSelect, required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["hostname"].widget.attrs.update(
            placeholder="mail.example.com", autocomplete="off", spellcheck="false"
        )

    def clean_hostname(self) -> str:
        return normalize_hostname(self.cleaned_data["hostname"])

    def clean(self):
        cleaned = super().clean()
        cleaned["goal"] = cleaned.get("goal") or Domain.Goal.RECEIVE
        if not cleaned.get("setup_mode"):
            self.add_error("setup_mode", "Choose direct MX or forwarding to receive email.")
        return cleaned


class RetentionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = RetentionPolicy
        fields = (
            "raw_message_days",
            "attachment_days",
            "normalized_content_days",
            "audit_metadata_days",
            "delivery_metadata_days",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class DraftRevisionForm(StyledFormMixin, forms.Form):
    expected_revision_id = forms.UUIDField(widget=forms.HiddenInput)
    subject = forms.CharField(max_length=998)
    body_text = forms.CharField(max_length=20000, widget=forms.Textarea(attrs={"rows": 14}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class ManualReplyForm(StyledFormMixin, forms.Form):
    context_message_id = forms.UUIDField(widget=forms.HiddenInput)
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)
    context_token = forms.CharField(max_length=1000, widget=forms.HiddenInput)
    draft_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    expected_revision_id = forms.UUIDField(required=False, widget=forms.HiddenInput)
    subject = forms.CharField(max_length=998, strip=False)
    body_text = forms.CharField(
        max_length=20000, label="Message", strip=False, widget=forms.Textarea(attrs={"rows": 5})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["body_text"].widget.attrs.update(placeholder="Write your reply…")


class DigestSettingsForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DigestSettings
        fields = ("enabled", "window_hours", "send_time", "timezone_name")
        labels = {
            "enabled": "Email me a digest",
            "window_hours": "Look back (hours)",
            "send_time": "Send daily at",
            "timezone_name": "Timezone",
        }
        widgets = {
            "send_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "timezone_name": forms.TextInput(attrs={"placeholder": "Europe/Istanbul"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
