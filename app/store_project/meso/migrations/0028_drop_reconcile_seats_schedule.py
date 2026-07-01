"""Drop the daily ``reconcile_seats`` schedule (flat plan, D14).

The flat monthly Pro plan (D14) removed per-seat billing, so there is no Stripe
seat quantity to reconcile — the ``meso-reconcile-seats`` sweep (registered by
``0021_register_reconcile_seats_schedule``) and its task/command are gone. Delete
the now-orphaned ``django_q.Schedule`` row so the ``qcluster`` doesn't fire a
missing task. Reversible: re-create it (mirroring 0021) if per-seat billing ever
returns.
"""

from django.db import migrations

NAME = "meso-reconcile-seats"
FUNC = "store_project.meso.tasks.reconcile_seats"


def remove_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=NAME).delete()


def recreate_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=NAME,
        defaults={"func": FUNC, "schedule_type": "D"},  # Schedule.DAILY
    )


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0027_proposedchange_membership_alter_proposedchange_kind"),
        ("django_q", "__latest__"),
    ]

    operations = [
        migrations.RunPython(remove_schedule, recreate_schedule),
    ]
