"""Register the daily invite-sweep schedules (django-q2).

Creates the ``django_q.Schedule`` rows that drive the two N4 invite sweeps so
scheduling is *versioned and deploys with the code* — no manual admin step, no
box cron. Idempotent (keyed on ``name``) and reversible. Depends on
``django_q``'s own migrations (``__latest__``) so the ``Schedule`` table exists.

Each schedule points at a stable wrapper in ``store_project.meso.tasks``; the
``qcluster`` worker runs them daily. A coach can still pause/retime them in the
admin afterwards — this one-time migration won't clobber later edits.
"""

from django.db import migrations

#: name -> dotted task path. DAILY ("D") cadence; the cluster anchors the first
#: run at ``next_run`` (defaults to creation time) and repeats every 24h.
SCHEDULES = {
    "meso-expire-invites": "store_project.meso.tasks.expire_invites",
    "meso-remind-expiring-invites": "store_project.meso.tasks.remind_expiring_invites",
}


def create_schedules(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    for name, func in SCHEDULES.items():
        Schedule.objects.update_or_create(
            name=name,
            defaults={"func": func, "schedule_type": "D"},  # Schedule.DAILY
        )


def remove_schedules(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name__in=list(SCHEDULES)).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0017_coachinvite_reminder_sent_at"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_schedules, remove_schedules),
    ]
