from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .names import clean_name


class UserNameForm(forms.Form):
    name = forms.CharField(
        label="Your name",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "meso-field"}),
    )

    def clean_name(self):
        return clean_name(self.cleaned_data["name"])


class CoachDisplayNameForm(forms.Form):
    display_name = forms.CharField(
        label="Display name",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "meso-field"}),
    )
    programming_style = forms.CharField(
        label="Programming style",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "meso-field",
                "placeholder": "Compound-first, RPE-based load",
            }
        ),
    )
    avoid_rules = forms.CharField(
        label="Avoid rules",
        max_length=2000,
        required=False,
        widget=forms.Textarea(
            attrs={"class": "meso-field", "maxlength": "2000", "rows": "4"}
        ),
    )

    def clean_display_name(self):
        return clean_name(self.cleaned_data["display_name"])

    def clean_programming_style(self):
        tags = []
        seen = set()
        for raw_tag in self.cleaned_data["programming_style"].split(","):
            tag = clean_name(raw_tag)
            if not tag:
                continue
            if len(tag) > 40:
                raise ValidationError(
                    "Each programming style tag must be 40 characters or fewer."
                )
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                tags.append(tag)
        if len(tags) > 12:
            raise ValidationError("Enter no more than 12 programming style tags.")
        return tags

    def clean_avoid_rules(self):
        return self.cleaned_data["avoid_rules"].strip()


class AthleteRecordForm(forms.Form):
    goals = forms.CharField(max_length=2000, required=False)
    training_started = forms.DateField(required=False)
    notes = forms.CharField(max_length=5000, required=False)

    def clean_goals(self):
        return self.cleaned_data["goals"].strip()

    def clean_training_started(self):
        started = self.cleaned_data["training_started"]
        if started is not None and started > timezone.localdate():
            raise ValidationError("Training started cannot be in the future.")
        return started

    def clean_notes(self):
        return self.cleaned_data["notes"].strip()


class ContraindicationForm(forms.Form):
    text = forms.CharField(required=False)

    def clean_text(self):
        text = clean_name(self.cleaned_data["text"])
        if not text:
            raise ValidationError("Enter a contraindication.")
        if len(text) > 255:
            raise ValidationError("Contraindications must be 255 characters or fewer.")
        return text
