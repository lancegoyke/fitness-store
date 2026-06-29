"""Scheduled task entry points for the Meso invite sweeps (django-q2).

These are the stable, importable callables that ``django_q.Schedule`` rows point
at (registered by migration ``0018_register_invite_schedules``). Each is a thin
wrapper over the existing management command so the sweep logic has a single home
(the command), runs identically from the CLI and the scheduler, and the dotted
path the schedule stores never has to track refactors of that logic.

The cluster (``manage.py qcluster``) executes these on the cadence the Schedule
rows define — daily today. Both commands are idempotent, so an extra run (e.g. a
deploy-day catch-up) is harmless.
"""

from django.core.management import call_command


def expire_invites():
    """Sweep overdue pending invites to ``expired`` (``meso_expire_invites``)."""
    call_command("meso_expire_invites")


def remind_expiring_invites():
    """Email reminders for invites nearing expiry (``meso_remind_expiring_invites``)."""
    call_command("meso_remind_expiring_invites")
