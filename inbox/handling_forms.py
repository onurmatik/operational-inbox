from django import forms
from django.forms import formset_factory

from inbox.models import HandlingPolicy
from inbox.services.handling_policy import ACTIONS, RISKS, TYPES
from inbox.services.tags import normalize_tag


class HandlingPolicyForm(forms.ModelForm):
    class Meta:
        model = HandlingPolicy
        fields = ("mode", "knowledge", "followup_enabled", "followup_hours")
        labels = {
            "mode": "Mode",
            "knowledge": "Domain knowledge",
            "followup_enabled": "Send one reminder",
            "followup_hours": "Hours before reminder and review",
        }
        widgets = {"knowledge": forms.Textarea(attrs={"rows": 5, "maxlength": 20000})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.domain_id is None:
            self.fields.pop("knowledge")
            mode = self.fields["mode"]
            assert isinstance(mode, forms.ChoiceField)
            mode.choices = [
                (v, label) for v, label in HandlingPolicy.Mode.choices if v != "inherit"
            ]
        for field in self.fields.values():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "oi-input"
        self.fields["followup_hours"].widget.attrs.update({"min": 1, "max": 720})


class HandlingRuleForm(forms.Form):
    name = forms.CharField(max_length=80, label="Name")
    types = forms.MultipleChoiceField(choices=[(v, v.title()) for v in TYPES], required=False)
    tags = forms.CharField(
        required=False, max_length=1200, help_text="Comma-separated; matches any tag."
    )
    risks = forms.MultipleChoiceField(choices=[(v, v.title()) for v in RISKS], required=False)
    action = forms.ChoiceField(
        initial="auto",
        choices=list(
            zip(ACTIONS, ("Auto handle", "Draft only", "Review", "Tag + archive"), strict=True)
        ),
    )
    add_tags = forms.CharField(required=False, max_length=1200, label="Add tags")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "oi-input"
        for name in ("types", "risks"):
            self.fields[name].widget.attrs["class"] = "oi-input h-auto min-h-24"

    def _tags(self, field):
        values = [normalize_tag(v)[0] for v in self.cleaned_data[field].split(",") if v.strip()]
        if len(values) > 20:
            raise forms.ValidationError("Use at most 20 tags.")
        return values

    def clean_tags(self):
        return self._tags("tags")

    def clean_add_tags(self):
        return self._tags("add_tags")


HandlingRuleFormSet = formset_factory(
    HandlingRuleForm,
    extra=1,
    can_order=True,
    can_delete=True,
    max_num=30,
    validate_max=True,
    absolute_max=40,
)
