from django.forms import ModelForm
from django.forms import TextInput

from .models import Challenge
from .models import Record


class ChallengeCreateForm(ModelForm):
    class Meta:
        model = Challenge
        fields = [
            "name",
            "description",
            "summary",
            "difficulty_level",
            "challenge_tags",
        ]
        widgets = {"challenge_tags": TextInput(attrs={"class": "input"})}


class RecordCreateForm(ModelForm):
    class Meta:
        model = Record
        fields = [
            "time_score",
            "notes",
        ]
