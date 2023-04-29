from django.contrib import admin

from .models import Alternative
from .models import Category
from .models import Exercise


class AlternativeInline(admin.TabularInline):
    """Tabular Inline View for Alternative."""

    model = Alternative
    extra = 1
    fk_name = "original"
    fields = (
        "problem",
        "alternate",
    )
    autocomplete_fields = [
        "alternate",
    ]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Category admin view."""

    list_display = ["name"]
    prepopulated_fields = {"slug": ("name",)}
    ordering = ["name"]


@admin.register(Exercise)
class ExerciseAdmin(admin.ModelAdmin):
    """Exercise admin view with Alternative inline."""

    list_display = [
        "name",
        "created",
        "modified",
    ]
    prepopulated_fields = {"slug": ("name",)}
    ordering = ["-created"]
    inlines = [
        AlternativeInline,
    ]
    search_fields = [
        "name",
    ]
