from datetime import timedelta

import django_filters
from django import forms
from django.db import models
from django.db.models import Count
from django.utils import timezone

from .models import Challenge
from .models import Record


class ChallengeFilter(django_filters.FilterSet):
    ordering = django_filters.ChoiceFilter(
        label="Sort by",
        choices=(
            ("popularity", "By Popularity"),
            ("name", "Alphabetical"),
            ("-date_created", "Newest First"),
            ("date_created", "Oldest First"),
        ),
        method="filter_by_ordering",
        empty_label=None,  # Remove duplicate alphabetical option
        initial="popularity",  # Default to popularity
    )

    def filter_by_ordering(self, queryset, name, value):
        # If no value provided, default to popularity
        if not value or value == '':
            value = 'popularity'
            
        if value == "popularity":
            # Get records from the last month
            one_month_ago = timezone.now() - timedelta(days=30)
            return queryset.annotate(
                record_count=Count(
                    "records", filter=models.Q(records__date_recorded__gte=one_month_ago)
                )
            ).order_by("-record_count", "name")
        elif value == "name":
            return queryset.order_by("name")
        elif value == "-date_created":
            return queryset.order_by("-date_created")
        elif value == "date_created":
            return queryset.order_by("date_created")
        return queryset

    class Meta:
        model = Challenge
        fields = {
            "name": ["icontains"],
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
