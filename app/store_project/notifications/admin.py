"""Admin for the e-mail delivery tracking models (issue #507, part 1).

Both models are system-written (``notifications.ses_events``, off SES/SNS
webhook events and the ``message_sent`` signal) — there is nothing a person
should hand-edit here, so both are read-only browsing surfaces pending the
staff deliverability dashboard (#507, part 2). Add, change, and delete are
all disabled; ``has_view_permission`` falls back to the ``view`` *or*
``change`` Django permission, so a staff user still gets read-only browsing
without needing the ``change`` permission specifically.
"""

from django.contrib import admin

from .models import EmailEvent
from .models import SentEmail


@admin.register(SentEmail)
class SentEmailAdmin(admin.ModelAdmin):
    list_display = ("recipient", "kind", "subject", "user", "sent_at")
    list_filter = ("kind",)
    search_fields = ("recipient", "ses_message_id", "subject")
    raw_id_fields = ("user",)
    readonly_fields = (
        "ses_message_id",
        "kind",
        "recipient",
        "user",
        "subject",
        "sent_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EmailEvent)
class EmailEventAdmin(admin.ModelAdmin):
    list_display = ("recipient", "event_type", "kind", "occurred_at")
    list_filter = ("event_type", "kind")
    search_fields = ("recipient", "ses_message_id", "sns_message_id")
    raw_id_fields = ("sent_email",)
    readonly_fields = (
        "sent_email",
        "event_type",
        "ses_message_id",
        "sns_message_id",
        "recipient",
        "kind",
        "occurred_at",
        "user_agent",
        "is_bot",
        "link",
        "bounce_type",
        "bounce_subtype",
        "raw",
        "created",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
