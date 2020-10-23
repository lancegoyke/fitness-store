from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MarketingConfig(AppConfig):
    name = "store_project.marketing"
    verbose_name = _("Marketing")
