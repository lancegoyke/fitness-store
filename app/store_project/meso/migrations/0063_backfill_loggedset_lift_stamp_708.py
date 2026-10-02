"""Backfill ``LoggedSet.exercise`` / ``exercise_name`` (#708).

``0062`` added the write-time lift stamp. Rows written before it have none, and
the best identity an old row has is the one every read gave it until now: its
current slot (``exercise_slot``, or for a row whose anchor is NULL the slot of
its ``prescription``). This pass stamps exactly that, so history that exists
today is frozen as it reads today and a later swap or rename can no longer
relabel it.

Idempotent: only rows with ``exercise_name IS NULL`` are touched, so a re-run
(or a run after a partial one) changes nothing already stamped.

Rows old code writes AFTER this runs (a still-running container during the
rolling deploy) stay NULL. That is expected and fine: ``LoggedSet.lift`` reads
an unstamped row through the anchor slot's live identity, permanently.

Forward-only (the reverse is a no-op: the columns are dropped by ``0062``'s
own reverse). Kept in its own file so the schema change and the data update
run in separate transactions on PostgreSQL.
"""

from django.db import migrations
from django.db.models import OuterRef
from django.db.models import Subquery


def backfill_lift_stamp(apps, schema_editor):
    LoggedSet = apps.get_model("meso", "LoggedSet")
    ExerciseSlot = apps.get_model("meso", "ExerciseSlot")
    alias = schema_editor.connection.alias

    by_anchor = ExerciseSlot.objects.using(alias).filter(
        pk=OuterRef("exercise_slot_id")
    )
    LoggedSet.objects.using(alias).filter(
        exercise_name__isnull=True, exercise_slot__isnull=False
    ).update(
        exercise_name=Subquery(by_anchor.values("name")[:1]),
        exercise_id=Subquery(by_anchor.values("exercise_id")[:1]),
    )

    by_cell = ExerciseSlot.objects.using(alias).filter(
        cells=OuterRef("prescription_id")
    )
    LoggedSet.objects.using(alias).filter(
        exercise_name__isnull=True,
        exercise_slot__isnull=True,
        prescription__isnull=False,
    ).update(
        exercise_name=Subquery(by_cell.values("name")[:1]),
        exercise_id=Subquery(by_cell.values("exercise_id")[:1]),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0062_loggedset_lift_stamp_708"),
    ]

    operations = [
        migrations.RunPython(
            backfill_lift_stamp, migrations.RunPython.noop, elidable=False
        ),
    ]
