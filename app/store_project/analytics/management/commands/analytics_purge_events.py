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

Each sweep is isolated (adversarial review of #509, fix 3): sharing one
command coupled the two ledgers only for scheduling, never for liveness, but
running them with no guard between them made the second sweep's fate depend
on the first's success — an exception in ``purge_expired_events`` used to
propagate straight out of ``handle()``, so ``purge_expired_push_notifications``
on the line right after it never ran that day, or any day after, for as long
as the first ledger's cause persisted. A broken table can't be allowed to
starve an unrelated one's retention sweep. So each sweep runs inside its own
``try``/``except``, and the command still exits non-zero (raises
``CommandError``, matching ``generate_summaries``'s per-item-failure pattern)
when either one failed, so a broken sweep is a loud cron/monitoring failure
rather than a silent no-op.
"""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from store_project.analytics.retention import purge_expired_events
from store_project.notifications.retention import purge_expired_push_notifications


class Command(BaseCommand):
    help = (
        "Delete Event and PushNotification rows older than the retention window "
        "(the two first-party usage ledgers, swept independently — one failing "
        "does not stop the other)."
    )

    def handle(self, *args, **options):
        events_deleted, events_error = self._sweep("Event", purge_expired_events)
        push_deleted, push_error = self._sweep(
            "PushNotification", purge_expired_push_notifications
        )

        if events_error is None:
            self.stdout.write(
                self.style.SUCCESS(f"Purged {events_deleted} expired event(s).")
            )
        if push_error is None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Purged {push_deleted} expired push notification(s)."
                )
            )

        failures = [
            f"{label}: {error}"
            for label, error in (
                ("Event", events_error),
                ("PushNotification", push_error),
            )
            if error is not None
        ]
        if failures:
            raise CommandError("Retention sweep failed for " + "; ".join(failures))

    def _sweep(self, label, purge_fn):
        """Run one ledger's ``purge_fn``, isolated from the other sweep's outcome.

        Returns ``(deleted_count, error)`` — ``error`` is ``None`` on success,
        else the caught exception. Catching here (fix 3) is what stops one
        ledger's failure from ever reaching the call to the other's sweep.
        """
        try:
            return purge_fn(), None
        except Exception as exc:  # a broken sweep must not block the other one
            self.stderr.write(self.style.ERROR(f"{label} sweep failed: {exc}"))
            return 0, exc
