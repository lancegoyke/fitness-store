from django.contrib import admin

from .models import Event


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    """Read-only: events are written by ``track()`` alone."""

    list_display = ("name", "actor", "subject_type", "subject_id", "source", "created")
    list_filter = ("name", "source")
    date_hierarchy = "created"
    raw_id_fields = ("actor",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
