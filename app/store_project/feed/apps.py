from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class FeedConfig(AppConfig):
    name = "store_project.feed"
    verbose_name = _("Feed")
