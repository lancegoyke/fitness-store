from django import forms
from django.utils.translation import gettext_lazy as _

from store_project.meals import models

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

    SEDENTARY = "sed"
    LOWACTIVE = "low"
    ACTIVE = "mid"
    HIGHACTIVE = "hi"
    ACTIVITY_LEVEL_CHOICES = [
        (SEDENTARY, _("Sedentary")),
        (LOWACTIVE, _("Low active")),
        (ACTIVE, _("Active")),
        (HIGHACTIVE, _("High active")),
    ]

    FAT_LOSS = "lose"
    MAINTENANCE = "keep"
    MUSCLE_GAIN = "gain"
    GOAL_CHOICES = [
        (FAT_LOSS, _("Fat loss")),
        (MAINTENANCE, _("Maintenance")),
        (MUSCLE_GAIN, _("Muscle gain")),
    ]

    height = forms.FloatField()
    height_unit = forms.ChoiceField(choices=HEIGHT_UNIT_CHOICES)
    weight = forms.FloatField()
    weight_unit = forms.ChoiceField(choices=WEIGHT_UNIT_CHOICES)
    age = forms.IntegerField()
    sex = forms.ChoiceField(choices=SEX_CHOICES)
    activity_level = forms.ChoiceField(choices=ACTIVITY_LEVEL_CHOICES)
    goal = forms.ChoiceField(choices=GOAL_CHOICES)


class MealForm(forms.ModelForm):
    class Meta:
        model = models.Meal
        fields = [
            "ingredients",
            "net_cals",
            "description",
            "fat",
            "cals",
            "carbs",
            "net_carbs",
            "fiber",
            "protein",
        ]


class IngredientForm(forms.ModelForm):
    class Meta:
        model = models.Ingredient
        fields = [
            "spoon_id",
            "name",
            "amount",
            "unit",
            "cals",
            "net_cals",
            "fat",
            "carbs",
            "fiber",
            "net_carbs",
            "protein",
        ]


class UnitAmountForm(forms.Form):
    ingredient_id = forms.CharField(widget=forms.HiddenInput())
    name = forms.CharField(widget=forms.HiddenInput())
    amount = forms.IntegerField()
    unit = forms.ChoiceField()

    def __init__(self, *args, **kwargs):
        ingredient_id = kwargs.pop("ingredient_id", None)
        name = kwargs.pop("name", None)
        units = kwargs.pop("units", None)
        super(UnitAmountForm, self).__init__(*args, **kwargs)
        if ingredient_id:
            self.fields["ingredient_id"].initial = ingredient_id
        if name:
            self.fields["name"].initial = name
        if units:
            self.fields["unit"].choices = units
