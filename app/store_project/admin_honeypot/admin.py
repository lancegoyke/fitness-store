from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from store_project.admin_honeypot.models import LoginAttempt


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "get_ip_address",
        "get_session_key",
        "timestamp",
        "get_path",
    )
    list_filter = ("timestamp",)
    readonly_fields = ("path", "username", "ip_address", "session_key", "user_agent")
    search_fields = ("username", "ip_address", "user_agent", "path")

    def get_actions(self, request):
        actions = super(LoginAttemptAdmin, self).get_actions(request)
        if "delete_selected" in actions:
            del actions["delete_selected"]
        return actions

    @admin.display(description=_("Session"))
    def get_session_key(self, instance):
        return '<a href="?session_key=%(key)s">%(key)s</a>' % {
            "key": instance.session_key
        }

    @admin.display(description=_("IP Address"))
    def get_ip_address(self, instance):
        return '<a href="?ip_address=%(ip)s">%(ip)s</a>' % {"ip": instance.ip_address}

    @admin.display(description=_("URL"))
    def get_path(self, instance):
        return '<a href="?path=%(path)s">%(path)s</a>' % {"path": instance.path}

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
