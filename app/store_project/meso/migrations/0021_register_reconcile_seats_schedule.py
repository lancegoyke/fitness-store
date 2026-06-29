"""Register the daily ``reconcile_seats`` schedule (django-q2).

The seat-quantity reconciliation sweep (S6 billing, Phase 2) runs daily on the
``qcluster``, the same way the N4 invite sweeps do (see
``0018_register_invite_schedules``): a versioned ``django_q.Schedule`` row that
deploys with the code — no manual admin step, no box cron. Idempotent (keyed on
``name``) and reversible; points at the stable wrapper in
``store_project.meso.tasks``.
"""

from django.db import migrations

NAME = "meso-reconcile-seats"
FUNC = "store_project.meso.tasks.reconcile_seats"


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={"func": FUNC, "schedule_type": "D"},  # Schedule.DAILY
    )


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0020_coachsubscription"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
