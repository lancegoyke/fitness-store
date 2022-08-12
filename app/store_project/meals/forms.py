from django import forms
from django.utils.translation import gettext_lazy as _


class MacroForm(forms.Form):
    """Calculate macros"""

    WEIGHT_METRIC = "kg"
    WEIGHT_IMPERIAL = "lbs"
    WEIGHT_UNIT_CHOICES = [
        (WEIGHT_METRIC, _("Kilograms")),
        (WEIGHT_IMPERIAL, _("Pounds")),
    ]

    HEIGHT_METRIC = "cm"
    HEIGHT_IMPERIAL = "in"
    HEIGHT_UNIT_CHOICES = [
        (HEIGHT_METRIC, _("Centimeters")),
        (HEIGHT_IMPERIAL, _("Inches")),
    ]

    SEX_M = "M"
    SEX_F = "F"
    SEX_CHOICES = [
        (SEX_M, _("Male")),
        (SEX_F, _("Female")),
    ]

    SEDENTARY = 0
    LOWACTIVE = 1
    ACTIVE = 2
    HIGHACTIVE = 3
    ACTIVITY_LEVEL_CHOICES = [
        (SEDENTARY, _("Sedentary")),
        (LOWACTIVE, _("Low active")),
        (ACTIVE, _("Active")),
        (HIGHACTIVE, _("High active")),
    ]

    FAT_LOSS = 0
    MAINTENANCE = 1
    MUSCLE_GAIN = 2
    GOAL_CHOICES = [
        (FAT_LOSS, _("Fat loss")),
        (MAINTENANCE, _("Maintenance")),
        (MUSCLE_GAIN, _("Muscle gain")),
    ]

    height = forms.DecimalField()
    height_unit = forms.ChoiceField(choices=HEIGHT_UNIT_CHOICES)
    weight = forms.DecimalField()
    weight_unit = forms.ChoiceField(choices=WEIGHT_UNIT_CHOICES)
    age = forms.IntegerField()
    sex = forms.ChoiceField(choices=SEX_CHOICES)
    activity_level = forms.ChoiceField(choices=ACTIVITY_LEVEL_CHOICES)
    goal = forms.ChoiceField(choices=GOAL_CHOICES)
