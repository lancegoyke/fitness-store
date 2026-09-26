from django import forms

from .names import clean_name


class UserNameForm(forms.Form):
    name = forms.CharField(label="Your name", max_length=255, required=False)

    def clean_name(self):
        return clean_name(self.cleaned_data["name"])


class CoachDisplayNameForm(forms.Form):
    display_name = forms.CharField(label="Display name", max_length=255, required=False)

    def clean_display_name(self):
        return clean_name(self.cleaned_data["display_name"])
