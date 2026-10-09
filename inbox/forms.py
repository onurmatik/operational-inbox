from __future__ import annotations

from django import forms
from django.core.validators import validate_email

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


class ComposeForm(StyledFormMixin, forms.Form):
    domain = forms.ModelChoiceField(queryset=Domain.objects.none(), empty_label="Select a domain")
    from_local_part = forms.CharField(max_length=64, label="From", strip=False)
    to_address = forms.CharField(
        max_length=320,
        label="To",
        strip=False,
        validators=[validate_email],
        widget=forms.EmailInput,
    )
    subject = forms.CharField(max_length=998, strip=False)
    body_text = forms.CharField(
        max_length=20000,
        label="Body",
        strip=False,
        widget=forms.Textarea(attrs={"rows": 12}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, domains, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].queryset = domains
        self._style_fields()
        for name, field in self.fields.items():
            if name != "idempotency_key":
                field.widget.attrs["class"] += " min-h-11"
                field.widget.attrs["aria-describedby"] = f"{name}-errors"
        self.fields["from_local_part"].widget.attrs.update(
            {
                "autocomplete": "off",
                "autocapitalize": "none",
                "spellcheck": "false",
                "class": "oi-input min-h-11 min-w-0 flex-1 rounded-r-none border-r-0",
                "aria-describedby": "from-suffix from_local_part-errors",
            }
        )
        self.fields["to_address"].widget.attrs.update(
            autocomplete="email", autocapitalize="none", spellcheck="false"
        )
        self.fields["body_text"].widget.attrs["class"] += " min-h-[280px] resize-y py-3 leading-6"

    def clean_from_local_part(self) -> str:
        local_part = self.cleaned_data["from_local_part"]
        if "@" in local_part:
            raise forms.ValidationError("Enter only the part before @.")
        return local_part
