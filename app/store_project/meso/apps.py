from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MesoConfig(AppConfig):
    name = "store_project.meso"
    verbose_name = _("Meso Program Designer")

    def ready(self):
        # Registers the CoachSubscription post_save receiver (#649).
        from .billing import activation  # noqa: F401
