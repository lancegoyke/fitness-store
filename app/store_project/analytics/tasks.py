"""Scheduled task entry point for the ``Event`` retention sweep (django-q2, #509).

The stable, importable callable a ``django_q.Schedule`` row points at
(registered by migration ``0002_register_event_retention_schedule``) — same
shape as ``meso/tasks.py``: a thin wrapper over the management command so the
sweep logic has one home, and the dotted path the schedule stores never has
to track a refactor of ``retention.py``.
"""

from django.core.management import call_command


def purge_expired_events():
    """Delete ``Event`` rows past the retention window (``analytics_purge_events``)."""
    call_command("analytics_purge_events")
