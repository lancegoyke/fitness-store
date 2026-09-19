"""Purge both first-party ledgers past their retention window (#509 slice 3).

Sweeps ``analytics.Event`` (13 months, ``analytics.retention``) and
``notifications.PushNotification`` (the same 13 months, imported from the same
place — ``notifications.retention``) in one run. The two ledgers share this
one command/schedule rather than each getting its own, so there is still only
the one daily ``django_q.Schedule`` row registered by
``analytics/migrations/0002_register_event_retention_schedule.py`` — adding
the push sweep here needed no new migration.

Idempotent and safe to run on a cron; a run that finds nothing to purge in
either table is a no-op.

    manage.py analytics_purge_events
"""

from django.core.management.base import BaseCommand

from store_project.analytics.retention import purge_expired_events
from store_project.notifications.retention import purge_expired_push_notifications


class Command(BaseCommand):
    help = (
        "Delete Event and PushNotification rows older than the retention window "
        "(the two first-party usage ledgers)."
    )

    def handle(self, *args, **options):
        events_deleted = purge_expired_events()
        push_deleted = purge_expired_push_notifications()
        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {events_deleted} expired event(s) and "
                f"{push_deleted} expired push notification(s)."
            )
        )
