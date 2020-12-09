from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CardioConfig(AppConfig):
    name = "store_project.cardio"
    verbose_name = _("Cardio Workouts")
