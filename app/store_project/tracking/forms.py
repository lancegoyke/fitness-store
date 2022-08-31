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


class LoadMeasureForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = LoadMeasure


class PowerMeasureForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = PowerMeasure


class DistanceMeasureForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DistanceMeasure


class DurationMeasureForm(_MeasureForm):
    class Meta(_MeasureForm.Meta):
        model = DurationMeasure
