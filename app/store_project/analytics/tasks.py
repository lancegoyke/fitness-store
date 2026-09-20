"""Scheduled task entry point for the retention sweep (django-q2, #509).

The stable, importable callable a ``django_q.Schedule`` row points at
(registered by migration ``0002_register_event_retention_schedule``) — same
shape as ``meso/tasks.py``: a thin wrapper over the management command so the
sweep logic has one home, and the dotted path the schedule stores never has
to track a refactor of ``retention.py``. The function name and dotted path
predate #509 slice 3, when the ``analytics_purge_events`` command it wraps
was widened to also sweep ``notifications.PushNotification`` (adversarial
review of #509, fix 4b) — kept as-is rather than renamed, since the schedule
row already points at this exact path and a rename would need a migration for
no behavioral gain.
"""

from django.core.management import call_command


def purge_expired_events():
    """Run ``analytics_purge_events`` — sweeps both first-party usage ledgers.

    Despite the name (kept for the registered schedule's dotted path — see the
    module docstring), this purges expired rows in ``analytics.Event`` *and*
    ``notifications.PushNotification``: the command sweeps each independently
    and raises ``CommandError`` (propagated here) if either failed, so a
    failure still surfaces through django-q2's own task-failure tracking.
    """
    call_command("analytics_purge_events")
