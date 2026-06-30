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


def reconcile_seats():
    """Correct any Stripe seat-quantity drift for paid coaches (``meso_reconcile_seats``).

    The daily backstop behind the inline best-effort seat sync (S6 billing Phase 2).
    """
    call_command("meso_reconcile_seats")


def agent_margin_alert():
    """Email the owner about paying coaches over the agent margin threshold.

    The monthly margin-alert sweep (agent-usage tracking Phase 3). Runs over the
    *previous* (closed) calendar month so the report covers a full month, not the
    partial current one (``meso_agent_margin_alert --last-month``).
    """
    call_command("meso_agent_margin_alert", "--last-month")
