import os

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.forms.models import BaseInlineFormSet
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models import Challenge
from .models import Record

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


class RecentRecordsInlineFormSet(BaseInlineFormSet):
    """Inline formset that shows only the most recent 150 records for a challenge.

    Always keeps old records intact by simply not including them in the formset.
    """

    def get_queryset(self):
        # Return cached queryset if available to avoid repeated queries during rendering
        if hasattr(self, "_cached_queryset"):
            return self._cached_queryset

        queryset = (
            super()
            .get_queryset()
            .select_related("user", "challenge")
            .order_by("-date_recorded")
        )
        # For unsaved parent, return none so only extra forms are shown
        if not getattr(self.instance, "pk", None):
            self._cached_queryset = queryset.none()
            return self._cached_queryset
        # Limit to most recent 150 to avoid too many fields on submit
        limited = queryset[:150]
        # Force evaluation once so further uses re-use the result cache
        list(limited)
        self._cached_queryset = limited
        return self._cached_queryset

    def _construct_form(self, i, **kwargs):
        """Disable the expensive 'user' widget for existing rows to avoid per-row lookups.

        Keep it enabled for the extra (empty) forms so you can add new records.
        """
        form = super()._construct_form(i, **kwargs)
        # Existing instance forms have a primary key; extras do not
        if getattr(form.instance, "pk", None):
            user_field = form.fields.get("user")
            if user_field:
                user_field.disabled = True
                # Replace the autocomplete widget with a simple Select preloaded
                # with the single current value to avoid an extra DB fetch.
                current_user_id = getattr(form.instance, "user_id", None)
                current_user_label = (
                    str(getattr(form.instance, "user", "")) if current_user_id else ""
                )
                user_field.widget = forms.Select(
                    choices=[(current_user_id, current_user_label)]
                )
        return form


class RecordInline(admin.TabularInline):
    model = Record
    formset = RecentRecordsInlineFormSet
    extra = 3
    autocomplete_fields = ("user",)
    fields = ("user", "time_score", "notes")
    verbose_name = "Record"
    verbose_name_plural = "Records (showing most recent 150 if many exist)"

    def get_max_num(self, request, obj=None, **kwargs):
        """Cap total forms to what we display (150) plus 3 extra for new entries."""
        if obj and obj.pk:
            return 153
        # For new challenges, allow a reasonable cap so we still see the 3 extra rows
        return self.extra


@admin.register(Challenge)
class ChallengeAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("name",)}
    change_form_template = "admin/challenges/challenge/change_form.html"
    inlines = [
        RecordInline,
    ]
    list_display = (
        "name",
        "description",
        "summary",
        "difficulty_level",
        "date_created",
    )
    list_filter = ("difficulty_level", "date_created")
    search_fields = ("name", "description", "summary")
    list_per_page = 25
    readonly_fields = ("record_management",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.prefetch_related("challenge_tags").with_completion_stats()

    # Use default get_object which already relies on get_queryset

    @admin.display(description="Record Management")
    def record_management(self, obj):
        """Display record count and link to manage all records."""
        if obj.pk:
            total_count = obj.records.count()
            url = (
                reverse("admin:challenges_record_changelist")
                + f"?challenge__id__exact={obj.pk}"
            )
            return format_html(
                "<strong>Total Records:</strong> {}<br>"
                '<a href="{}" target="_blank">Manage All Records for this Challenge</a>',
                total_count,
                url,
            )
        return "Save to see record management options"

    def get_urls(self):
        from django.urls import path

        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/generate-summary/",
                self.admin_site.admin_view(self.generate_summary),
                name="challenge-generate-summary",
            ),
        ]
        return custom + urls

    def generate_summary(self, request, object_id):
        challenge = self.get_object(request, object_id)
        if not challenge:
            messages.error(request, "Challenge not found")
            return redirect("..")

        if challenge.summary:
            messages.info(request, "Summary already exists")
            return redirect("..")

        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            messages.error(request, "GOOGLE_API_KEY not configured")
            return redirect("..")

        try:

            def generate_challenge_summary(description, api_key):
                """Generate a summary of a challenge description using the Google Gemini API."""
                from google import genai

                client = genai.Client(api_key=api_key)
                prompt = (
                    "Summarize the following challenge description in no more than 300 characters: "
                    + description
                )
                response = client.models.generate_content(
                    model=DEFAULT_GEMINI_MODEL, contents=prompt
                )
                return response.text[:300]

            summary = generate_challenge_summary(challenge.description, api_key)
            challenge.summary = summary
            challenge.save()
            messages.success(request, "Summary generated")
        except Exception as exc:  # pragma: no cover - external API errors
            messages.error(request, f"Failed to generate summary: {exc}")
        return redirect("..")


@admin.register(Record)
class RecordAdmin(admin.ModelAdmin):
    list_display = ("challenge", "user", "time_score", "date_recorded", "date_updated")
    list_filter = ("date_recorded", "date_updated", "challenge__difficulty_level")
    search_fields = ("challenge__name", "user__name", "user__email")
    raw_id_fields = ("user", "challenge")
    list_select_related = ("user", "challenge")
    list_per_page = 50
    readonly_fields = ("date_recorded", "date_updated")

    fieldsets = (
        (None, {"fields": ("challenge", "user", "time_score", "notes")}),
        (
            "Timestamps",
            {
                "fields": ("date_recorded", "date_updated"),
                "classes": ("collapse",),
            },
        ),
    )
