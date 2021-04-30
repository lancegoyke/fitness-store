from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class NotificationsConfig(AppConfig):
    name = "store_project.notifications"
    verbose_name = _("Notifications")
