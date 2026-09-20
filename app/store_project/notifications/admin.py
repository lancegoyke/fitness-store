"""Admin for the notification tracking models (issue #507, part 1; #509 slice 3).

All three models are system-written — ``EmailEvent``/``SentEmail`` by
``notifications.ses_events`` off SES/SNS webhook events and the
``message_sent`` signal, ``PushNotification`` by ``notifications.push`` off
``meso.push``'s send path — so there is nothing a person should hand-edit
here; all three are read-only browsing surfaces feeding the staff
deliverability dashboard (#507 part 2 for email, #509 slice 3 for push). Add,
change, and delete are all disabled; ``has_view_permission`` falls back to the
``view`` *or* ``change`` Django permission, so a staff user still gets
read-only browsing without needing the ``change`` permission specifically.
"""

from django.contrib import admin

from .models import EmailEvent
from .models import PushNotification
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


@admin.register(PushNotification)
class PushNotificationAdmin(admin.ModelAdmin):
    list_display = ("kind", "user", "sent_at", "clicked_at", "error")
    list_filter = ("kind",)
    search_fields = ("id", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("id", "kind", "user", "sent_at", "error", "clicked_at")

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
