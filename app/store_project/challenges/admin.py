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
