"""Register the hourly 24h-settle schedule (5b, settle.py).

Creates the ``django_q.Schedule`` row that sweeps quiet PENDING session logs
to DONE, mirroring ``0030_register_sandbox_expiry_schedule`` (and, further
back, ``0018_register_invite_schedules``): scheduling is versioned and
deploys with the code rather than living as a hand-rolled box cron. Hourly,
because the quiet window is measured in hours (``MESO_SETTLE_QUIET_HOURS``)
and a stale PENDING log is invisible to every DONE-only read until it
settles. Idempotent (keyed on ``name``) and reversible. Depends on
``django_q``'s own migrations, pinned to its 0019 leaf rather than
``__latest__`` (which re-resolves every graph build and would break
``migrate`` once django-q2 ships a new migration, #552), so the
``Schedule`` table exists, and on 0046 so the ``last_activity_at`` column the
sweep reads exists too.
"""

from datetime import timedelta

from django.db import migrations
from django.utils import timezone

NAME = "meso-settle-logs"
FUNC = "store_project.meso.tasks.settle_logs"


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={
            "func": FUNC,
            "schedule_type": "H",  # Schedule.HOURLY
            # First run an hour out, not "now": a deploy migrates before it
            # replaces the running qcluster, and the old worker would pick up a
            # due row it can't import (`settle_logs` doesn't exist in its code)
            # and record a failed task. Nothing can settle in that first hour
            # anyway — 0046 stamps existing logs with the migration time.
            "next_run": timezone.now() + timedelta(hours=1),
        },
    )


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0046_sessionlog_last_activity_at"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
