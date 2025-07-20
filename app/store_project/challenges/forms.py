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
            "difficulty_level",
            "tags",
        ]
        widgets = {"tags": TextInput(attrs={"class": "input"})}


class RecordCreateForm(ModelForm):
    class Meta:
        model = Record
        fields = [
            "time_score",
            "notes",
        ]
