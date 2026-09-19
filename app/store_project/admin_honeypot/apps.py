from django.apps import AppConfig


class AdminHoneypotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "store_project.admin_honeypot"

    def ready(self):
        # Connects notify_admins() to the honeypot signal (issue #514) --
        # listeners.py's own honeypot.connect() call never runs otherwise.
        from . import listeners  # noqa: F401
