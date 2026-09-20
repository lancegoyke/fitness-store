"""Backfill ``LoggedSet.exercise_slot`` from ``prescription`` (#578 C1).

``exercise_slot`` (added in ``0050``) is the durable anchor a ``LoggedSet``
should have carried from the start; this pass fills it for every row that
already has a live ``prescription`` to read the slot off of. A joined
``F()`` can't cross tables inside ``update()``, so a ``Subquery`` does the
join instead — this runs identically on SQLite and PostgreSQL, unlike a
raw correlated-update SQL string would.

A row can be left with ``exercise_slot`` still NULL after this — its
``prescription`` was already NULL (hard-deleted before this migration ever
ran, e.g. by #577's now-fixed purge or #581's unguarded admin delete). There
is no slot to recover it from: guessing one from ``source_line`` or
``reclaimed_line`` would fabricate an identity this migration didn't
actually observe, so instead it *reports* the leftover pks (capped, so a
huge count doesn't flood the deploy log) and leaves them NULL. The
transitional ``LoggedSet.anchor_slot`` fallback (``models.py``) keeps these
rows counting toward 1RM/PRs in the meantime, off their still-live
``prescription``, if it survived.

Forward-only: there is nothing meaningful to "undo" — reversing would mean
re-nulling a column that (going forward) every write path fills, and a
schema rollback belongs to ``0050``, not here. ``elidable=False`` because
this is exactly the row-count-dependent case that must never be squashed
away silently: this migration's behavior (what it fills vs. what it
reports) depends on the data present at *this* point in history, and a
squash that skips it would silently change what a fresh-DB migrate leaves
NULL.
"""

from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill_exercise_slot(apps, schema_editor):
    LoggedSet = apps.get_model("meso", "LoggedSet")
    Prescription = apps.get_model("meso", "Prescription")

    LoggedSet.objects.filter(
        exercise_slot__isnull=True, prescription__isnull=False
    ).update(
        exercise_slot_id=Subquery(
            Prescription.objects.filter(pk=OuterRef("prescription_id")).values(
                "exercise_slot_id"
            )[:1]
        )
    )

    # Whatever is still NULL has no ``prescription`` to have backfilled it
    # from (see the module docstring) — report it rather than guess, so an
    # operator can chase these pks down (they're the pre-existing #577/#581
    # casualties this whole change exists to stop producing more of).
    leftover_pks = list(
        LoggedSet.objects.filter(exercise_slot__isnull=True)
        .order_by("pk")
        .values_list("pk", flat=True)[:20]
    )
    remaining = LoggedSet.objects.filter(exercise_slot__isnull=True).count()
    if remaining:
        print(
            f"0051_backfill_loggedset_exercise_slot: {remaining} LoggedSet "
            "row(s) have no prescription to backfill exercise_slot from "
            f"(first {len(leftover_pks)} pks): {leftover_pks}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("meso", "0050_loggedset_exercise_slot"),
    ]

    operations = [
        migrations.RunPython(
            backfill_exercise_slot, migrations.RunPython.noop, elidable=False
        ),
    ]
