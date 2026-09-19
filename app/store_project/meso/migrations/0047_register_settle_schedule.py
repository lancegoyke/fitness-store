"""Register the hourly 24h-settle schedule (5b, settle.py).

Creates the ``django_q.Schedule`` row that sweeps quiet PENDING session logs
to DONE, mirroring ``0030_register_sandbox_expiry_schedule`` (and, further
back, ``0018_register_invite_schedules``): scheduling is versioned and
deploys with the code rather than living as a hand-rolled box cron. Hourly,
because the quiet window is measured in hours (``MESO_SETTLE_QUIET_HOURS``)
and a stale PENDING log is invisible to every DONE-only read until it
settles. Idempotent (keyed on ``name``) and reversible. Depends on
``django_q``'s own migrations (``__latest__``) so the ``Schedule`` table
exists, and on 0046 so the ``last_activity_at`` column the sweep reads exists
too.
"""

from django.db import migrations

NAME = "meso-settle-logs"
FUNC = "store_project.meso.tasks.settle_logs"


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={"func": FUNC, "schedule_type": "H"},  # Schedule.HOURLY
    )


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0046_sessionlog_last_activity_at"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
