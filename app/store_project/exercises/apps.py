from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ExercisesConfig(AppConfig):
    name = "store_project.exercises"
    verbose_name = _("Exercises")
