from django import forms
from django.forms.models import inlineformset_factory

from .models import (
    DistanceMeasure,
    DurationMeasure,
    LoadMeasure,
    PowerMeasure,
    Test,
)


class TestForm(forms.ModelForm):
    class Meta:
        model = Test
        fields = (
            "name",
            "slug",
            "description",
            "video",
            "author",
            "category",
        )


class _MeasureForm(forms.ModelForm):
    """
    For recording test measurements

    This form is abstract and should not be rendered directly.
    Make a subclassed form instead.
    """

    class Meta:
        fields = ("test", "user", "value", "unit")


class _MeasureTestForm(forms.ModelForm):
    """
    For recording measurements for a given test

    This form is abstract and should not be rendered directly.
    Make a subclassed form instead.
    """

    class Meta:
        fields = ("user", "value", "unit")


class _MeasureAthleteTestForm(forms.ModelForm):
    """
    For recording measurements for a given test

    This form is abstract and should not be rendered directly.
    Make a subclassed form instead.
    """

    class Meta:
        fields = ("value", "unit")


class LoadMeasureBaseForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = LoadMeasure


class LoadMeasureTestForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = LoadMeasure
        fields = ("value", "unit")


class LoadMeasureStaffForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = LoadMeasure
        fields = ("user", "value", "unit")


LoadMeasureTestFormSet = inlineformset_factory(
    Test,
    LoadMeasure,
    form=LoadMeasureStaffForm,
    can_delete=False,
)


class PowerMeasureBaseForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = PowerMeasure


class PowerMeasureTestForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = PowerMeasure
        fields = ("value", "unit")


class PowerMeasureStaffForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = PowerMeasure
        fields = ("user", "value", "unit")


PowerMeasureTestFormSet = inlineformset_factory(
    Test,
    PowerMeasure,
    form=PowerMeasureStaffForm,
    can_delete=False,
)


class DistanceMeasureBaseForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DistanceMeasure


class DistanceMeasureTestForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DistanceMeasure
        fields = ("value", "unit")


class DistanceMeasureStaffForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DistanceMeasure
        fields = ("user", "value", "unit")


DistanceMeasureTestFormSet = inlineformset_factory(
    Test,
    DistanceMeasure,
    form=DistanceMeasureStaffForm,
    can_delete=False,
)


class DurationMeasureBaseForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DurationMeasure


class DurationMeasureTestForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DurationMeasure
        fields = ("value", "unit")


class DurationMeasureStaffForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DurationMeasure
        fields = ("user", "value", "unit")


DurationMeasureTestFormSet = inlineformset_factory(
    Test,
    DurationMeasure,
    form=DurationMeasureStaffForm,
    can_delete=False,
)
