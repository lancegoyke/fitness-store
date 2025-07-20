import django_filters
from django import forms

from .models import Challenge
from .models import Record


class ChallengeFilter(django_filters.FilterSet):
    tags = django_filters.CharFilter(
        field_name="tags__name", lookup_expr="icontains", label="Tags"
    )

    ordering = django_filters.OrderingFilter(
        choices=(
            ("-date_created", "Newest First"),
            ("date_created", "Oldest First"),
        ),
        fields={
            "date_created": "Date",
        },
    )

    class Meta:
        model = Challenge
        fields = {
            "name": ["icontains"],
            "tags": ["exact"],
        }


class RecordFilter(django_filters.FilterSet):
    order = django_filters.OrderingFilter(
        label="Sort",
        choices=(
            ("time", "Fastest"),
            ("-time", "Slowest"),
            ("-when", "Newest"),
            ("when", "Oldest"),
        ),
        fields=(
            ("date_recorded", "when"),
            ("time_score", "time"),
        ),
        field_labels={
            "date_recorded": "Oldest",
            "-date_recorded": "Newest",
            "time_score": "Fastest",
            "-time_score": "Slowest",
        },
    )
    username_filter = django_filters.CharFilter(
        label="Username Filter",
        field_name="user__username",
        lookup_expr="icontains",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )

    class Meta:
        model = Record
        fields = []
