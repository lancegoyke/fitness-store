"""Register the daily Event-retention schedule (#509).

Creates the ``django_q.Schedule`` row that sweeps ``Event`` rows past the
13-month retention window (``retention.py``), mirroring
``meso/migrations/0047_register_settle_schedule.py``: scheduling is versioned
and deploys with the code rather than living as a hand-rolled box cron.
Daily, since the retention window is measured in months and there's no cost
to a day's lag. Idempotent (keyed on ``name``) and reversible. Depends on
``django_q``'s own migrations, pinned to its 0019 leaf rather than
``__latest__`` (which re-resolves every graph build and would break
``migrate`` once django-q2 ships a new migration, #552), so the
``Schedule`` table exists.
"""

from datetime import timedelta

from django.db import migrations
from django.utils import timezone

NAME = "analytics-purge-expired-events"
FUNC = "store_project.analytics.tasks.purge_expired_events"


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={
            "func": FUNC,
            "schedule_type": "D",  # Schedule.DAILY
            # First run an hour out, not "now": a deploy migrates before it
            # replaces the running qcluster, and the old worker would pick up a
            # due row it can't import (`purge_expired_events` doesn't exist in
            # its code) and record a failed task.
            "next_run": timezone.now() + timedelta(hours=1),
        },
    )


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0001_initial"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
