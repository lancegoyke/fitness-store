"""Register the monthly ``agent_margin_alert`` schedule (django-q2).

The agent margin-alert sweep (agent-usage tracking Phase 3) runs **monthly** on
the ``qcluster`` — unlike the daily invite/seat sweeps — because it summarizes a
full closed month of agent cost vs revenue (the task reports the *previous*
month). A versioned ``django_q.Schedule`` row that deploys with the code, like
``0018_register_invite_schedules`` and ``0021_register_reconcile_seats_schedule``:
no manual admin step, no box cron. Idempotent (keyed on ``name``) and reversible;
points at the stable wrapper in ``store_project.meso.tasks``.
"""

from django.db import migrations

NAME = "meso-agent-margin-alert"
FUNC = "store_project.meso.tasks.agent_margin_alert"


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={"func": FUNC, "schedule_type": "M"},  # Schedule.MONTHLY
    )


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0025_agentproposalbatch_api_calls_and_more"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
