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
    db_alias = schema_editor.connection.alias

    LoggedSet.objects.using(db_alias).filter(
        exercise_slot__isnull=True, prescription__isnull=False
    ).update(
        exercise_slot_id=Subquery(
            # ``.order_by()`` (clearing it) matters here: ``Prescription.Meta.
            # ordering = ["exercise_slot__order", "line"]`` otherwise leaks
            # into this SET subquery and makes the emitted SQL carry an INNER
            # JOIN on ``meso_exerciseslot`` (to sort by ``U1."order"``) that
            # has nothing to do with the value being selected. Harmless today
            # only because ``Prescription.exercise_slot`` is ``null=False`` —
            # the INNER JOIN can never drop a row. If that field ever became
            # nullable, the implicit join would turn into a filter and a
            # ``Prescription`` with no ``exercise_slot`` would make this
            # subquery return no row at all, so the ``UPDATE`` would write
            # NULL for that ``LoggedSet`` instead of leaving it alone (or
            # correctly resolving it) — a silent behavior change hiding
            # inside an ORDER BY that was never meant to affect this query.
            Prescription.objects.using(db_alias)
            .filter(pk=OuterRef("prescription_id"))
            .order_by()
            .values("exercise_slot_id")[:1]
        )
    )

    # Whatever is still NULL either (a) never had a ``prescription`` to
    # backfill from (a genuine #577/#581 casualty — hard-deleted before this
    # migration ever ran), or (b) raced the ``UPDATE`` above: under Postgres
    # READ COMMITTED, a still-running old container can INSERT a fresh
    # ``LoggedSet`` (``exercise_slot`` NULL, ``prescription`` live) in the gap
    # between the ``UPDATE`` above and the counts below, during a rolling
    # deploy. That row's ``prescription`` is perfectly live — it just missed
    # the ``UPDATE`` sweep — so it is NOT a #577/#581 casualty, and reporting
    # it as one would send an operator chasing a data-loss bug that isn't
    # there. The two are told apart by whether ``prescription`` itself is
    # NULL: only that half is unrecoverable, so only that half is the
    # headline count. The other half is real but expected to self-heal —
    # named separately, as rows the follow-up's re-run will pick up.
    unrecoverable_qs = LoggedSet.objects.using(db_alias).filter(
        exercise_slot__isnull=True, prescription__isnull=True
    )
    unrecoverable_pks = list(
        unrecoverable_qs.order_by("pk").values_list("pk", flat=True)[:20]
    )
    unrecoverable = unrecoverable_qs.count()
    if unrecoverable:
        print(
            f"0051_backfill_loggedset_exercise_slot: {unrecoverable} LoggedSet "
            "row(s) have no prescription to backfill exercise_slot from "
            f"(first {len(unrecoverable_pks)} pks): {unrecoverable_pks}"
        )

    still_null = (
        LoggedSet.objects.using(db_alias).filter(exercise_slot__isnull=True).count()
    )
    raced = still_null - unrecoverable
    if raced:
        print(
            f"0051_backfill_loggedset_exercise_slot: {raced} additional "
            "LoggedSet row(s) still have exercise_slot NULL but a LIVE "
            "prescription — these raced this migration's UPDATE (e.g. a "
            "still-running old container inserted them mid-deploy) rather "
            "than predating it, and the follow-up backfill re-run will pick "
            "them up."
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
