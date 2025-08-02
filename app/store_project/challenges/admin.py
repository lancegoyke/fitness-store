import os

from django.contrib import admin
from django.contrib import messages
from django.shortcuts import redirect

from .models import Challenge
from .models import Record

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


# Register your models here.
class RecordInline(admin.TabularInline):
    model = Record
    extra = 3
    list_per_page = 20
    raw_id_fields = ("user",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.select_related("user", "challenge").order_by("-date_recorded")


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

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset.prefetch_related(
            "challenge_tags", "records__user"
        ).with_completion_stats()

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj and hasattr(obj, "_prefetched_objects_cache"):
            return obj
        queryset = self.get_queryset(request)
        return queryset.get(pk=object_id)

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
    list_display = ("challenge", "user", "time_score", "date_recorded")
    list_filter = ("date_recorded", "challenge__difficulty_level")
    search_fields = ("challenge__name", "user__name", "user__email")
    raw_id_fields = ("user", "challenge")
    list_select_related = ("user", "challenge")
    list_per_page = 50
