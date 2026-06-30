from django.forms import ModelForm
from django.forms import TextInput

from .form_styling import apply_component_classes
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
        # Keep the tags field a free-text input (the model field is M2M, whose
        # default widget is a multi-select list box we don't want here).
        widgets = {"challenge_tags": TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_component_classes(self.fields)


class RecordCreateForm(ModelForm):
    class Meta:
        model = Record
        fields = [
            "time_score",
            "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_component_classes(self.fields)
