from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _


class CardioCreateForm(forms.Form):
    # Work and rest must take up same number of characters
    # for accurate string slices in cardio.views.cardio_create
    PROTOCOL_CHOICES = (  # in seconds
        (
            "Anaerobic",
            (
                ("3030", "30 seconds of work with 30 seconds of rest"),
                ("2040", "20 seconds of work with 40 seconds of rest"),
                ("1545", "15 seconds of work with 45 seconds of rest"),
                ("1248", "12 seconds of work with 48 seconds of rest"),
                ("1050", "10 seconds of work with 50 seconds of rest"),
                ("0852", "8 seconds of work with 52 seconds of rest"),
                ("0630", "6 seconds of work with 30 seconds of rest"),
                ("060240", "1 minute of work with 4 minutes of rest"),
            ),
        ),
        (
            "Aerobic",
            (
                ("4515", "45 seconds of work with 15 seconds of rest"),
                ("6030", "60 seconds of work with 30 seconds of rest"),
                ("060060", "1 minute of work with 1 minute of rest"),
                ("060120", "1 minute of work with 2 minutes of rest"),
                ("120060", "2 minutes of work with 1 minute of rest"),
                ("180120", "3 minutes of work with 2 minutes of rest"),
                ("180180", "3 minutes of work with 3 minutes of rest"),
                ("300120", "5 minutes of work with 2 minutes of rest"),
                ("360180", "6 minutes of work with 3 minutes of rest"),
                ("cont", "continuous activity"),
            ),
        ),
    )

    mode = forms.CharField(
        label="Exercise",
        widget=forms.TextInput(
            attrs={"placeholder": "e.g., running, biking, hiking, etc."}
        )
    )
    duration = forms.IntegerField(
        help_text="in minutes",
        widget=forms.NumberInput(
            attrs={"placeholder": "...in minutes"}
        )
    )
    protocol = forms.ChoiceField(
        choices=PROTOCOL_CHOICES,
        label="Interval",
    )

    def clean_duration(self):
        data = self.cleaned_data["duration"]

        # Check if duration is shorter than minimum time
        if data < 1:
            raise ValidationError(
                _("Duration is too short for intervals. Go sprint on a machine!")
            )

        # Check if duration is too long
        if data > 180:
            raise ValidationError(
                _(
                    "That's a pretty long workout, don't you think? Try a shorter duration."
                )
            )

        # Remember to always return the cleaned data
        return data
