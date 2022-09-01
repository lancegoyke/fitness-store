from django import forms

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
            "video_link",
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
