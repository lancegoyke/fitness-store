"""Register the hourly sandbox-expiry schedule (issue #389, Phase 2).

Creates the ``django_q.Schedule`` row that reaps expired ``/meso/demo/``
sandboxes (throwaway coach + demo-athlete users + seeded data) so scheduling is
*versioned and deploys with the code* — mirroring
``0018_register_invite_schedules``. Hourly, because the TTL is measured in hours
(``MESO_SANDBOX_TTL_HOURS``) and stale sandboxes are pure DB weight. Idempotent
(keyed on ``name``) and reversible. Depends on ``django_q``'s own migrations
(``__latest__``) so the ``Schedule`` table exists.
"""

from django.db import migrations

NAME = "meso-expire-sandboxes"
FUNC = "store_project.meso.tasks.expire_sandboxes"


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
        ("meso", "0029_sandboxsession"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
