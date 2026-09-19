"""Purge ``Event`` rows past the 13-month retention window (#509).

Idempotent and safe to run on a cron; a run that finds nothing to purge is a
no-op.

    manage.py analytics_purge_events
"""

from django.core.management.base import BaseCommand

from store_project.analytics.retention import purge_expired_events


class Command(BaseCommand):
    help = "Delete first-party analytics Event rows older than the retention window."

    def handle(self, *args, **options):
        deleted = purge_expired_events()
        self.stdout.write(self.style.SUCCESS(f"Purged {deleted} expired event(s)."))
