from __future__ import annotations

from django import forms

from inbox.models import Domain, RetentionPolicy, User
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
    setup_mode = forms.ChoiceField(choices=Domain.SetupMode.choices, widget=forms.RadioSelect)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields["hostname"].widget.attrs.update(
            placeholder="mail.example.com", autocomplete="off", spellcheck="false"
        )

    def clean_hostname(self) -> str:
        return normalize_hostname(self.cleaned_data["hostname"])


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
    subject = forms.CharField(max_length=998)
    body_text = forms.CharField(max_length=20000, widget=forms.Textarea(attrs={"rows": 14}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
