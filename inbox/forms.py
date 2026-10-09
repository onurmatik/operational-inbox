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
    goal = forms.ChoiceField(choices=Domain.Goal.choices, widget=forms.RadioSelect, required=False)
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
        if cleaned["goal"] == Domain.Goal.SEND:
            cleaned["setup_mode"] = Domain.SetupMode.DIRECT_MX
        elif not cleaned.get("setup_mode"):
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
    subject = forms.CharField(max_length=998)
    body_text = forms.CharField(max_length=20000, widget=forms.Textarea(attrs={"rows": 14}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class ComposeForm(StyledFormMixin, forms.Form):
    from_address = forms.CharField(
        max_length=320,
        label="From",
        required=False,
        strip=False,
        validators=[validate_email],
        widget=forms.EmailInput,
    )
    domain = forms.ModelChoiceField(
        queryset=Domain.objects.none(), required=False, widget=forms.HiddenInput
    )
    from_local_part = forms.CharField(
        max_length=64, strip=False, required=False, widget=forms.HiddenInput
    )
    to_address = forms.CharField(
        max_length=320,
        label="To",
        strip=False,
        validators=[validate_email],
        widget=forms.EmailInput,
    )
    subject = forms.CharField(max_length=998, strip=False)
    body_text = forms.CharField(
        max_length=20000, label="Message", strip=False, widget=forms.Textarea(attrs={"rows": 10})
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, domains, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domain"].queryset = domains
        self._style_fields()
        for name in ("from_address", "to_address"):
            self.fields[name].widget.attrs.update(
                autocomplete="email", autocapitalize="none", spellcheck="false"
            )
        self.fields["from_address"].widget.attrs.update(
            placeholder="hello@yourdomain.com", list="sender-addresses"
        )
        self.fields["body_text"].widget.attrs["class"] += " py-3 leading-6 resize-y"

    def clean(self):
        cleaned = super().clean()
        address = cleaned.get("from_address", "")
        domain = cleaned.get("domain")
        local = cleaned.get("from_local_part", "")
        if not address and domain and local:
            address = f"{local}@{domain.hostname}"
            try:
                validate_email(address)
            except forms.ValidationError:
                self.add_error("from_address", "Enter a valid From address.")
                return cleaned
        if not address:
            if "from_address" not in self.errors:
                self.add_error("from_address", "Enter a full From address.")
            return cleaned
        hostname = address.rsplit("@", 1)[1].casefold()
        selected = self.fields["domain"].queryset.filter(hostname=hostname).first()
        if (
            selected is None
            or (domain and selected.id != domain.id)
            or (local and address != f"{local}@{hostname}")
        ):
            self.add_error("from_address", "Use an address on one of your connected domains.")
        else:
            cleaned["domain"] = selected
            cleaned["from_address"] = address
        return cleaned
