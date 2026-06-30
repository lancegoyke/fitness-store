"""Register the monthly ``agent_margin_alert`` schedule (django-q2).

The agent margin-alert sweep (agent-usage tracking Phase 3) runs **monthly** on
the ``qcluster`` — unlike the daily invite/seat sweeps — because it summarizes a
full closed month of agent cost vs revenue (the task reports the *previous*
month). A versioned ``django_q.Schedule`` row that deploys with the code, like
``0018_register_invite_schedules`` and ``0021_register_reconcile_seats_schedule``:
no manual admin step, no box cron. Idempotent (keyed on ``name``) and reversible;
points at the stable wrapper in ``store_project.meso.tasks``.

Unlike the daily sweeps, this one **anchors ``next_run`` to the 1st of next
month**. django-q defaults an unset ``next_run`` to row-creation time, which for a
daily sweep just means "run on deploy then daily" (harmless, idempotent). For a
*monthly* sweep that would fire mid-deploy (an unexpected owner email) and then on
the deploy day-of-month — so a month's report could lag ~30 days. Anchoring to the
month boundary makes each just-closed month report land at the start of the next.
"""

from datetime import datetime

from django.db import migrations
from django.utils import timezone

NAME = "meso-agent-margin-alert"
FUNC = "store_project.meso.tasks.agent_margin_alert"


def _first_of_next_month():
    """The 1st of next month at 08:00 in the current timezone (the first fire)."""
    now = timezone.localtime(timezone.now())
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    naive = datetime(year, month, 1, 8, 0)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def create_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={
            "func": FUNC,
            "schedule_type": "M",  # Schedule.MONTHLY
            "next_run": _first_of_next_month(),
        },
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
