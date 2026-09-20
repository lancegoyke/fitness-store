"""Backfill ``LoggedSet.exercise_slot`` from ``prescription`` (#578 C1).

``exercise_slot`` (added in ``0050``) is the durable anchor a ``LoggedSet``
should have carried from the start; this pass fills it for every row that
already has a live ``prescription`` to read the slot off of. A joined
``F()`` can't cross tables inside ``update()``, so a ``Subquery`` does the
join instead — this runs identically on SQLite and PostgreSQL, unlike a
raw correlated-update SQL string would.

A row can be left with ``exercise_slot`` still NULL after this, for one of
two reasons, told apart by whether its ``prescription`` is itself NULL:

- **unrecoverable**: has no ``prescription`` at the moment of this read (e.g.
  hard-deleted before this migration ever ran, by #577's now-fixed purge or
  #581's unguarded admin delete — but not necessarily: a row inserted after
  the ``UPDATE`` below whose ``prescription`` is then hard-deleted before
  this migration's own snapshot read runs lands here too, even though its
  ``prescription`` was perfectly live when the ``UPDATE`` ran). There is no
  slot to recover it from either way: guessing one from ``source_line`` or
  ``reclaimed_line`` would fabricate an identity this migration didn't
  actually observe.
- **raced**: ``prescription`` is still live, but the row missed the
  ``UPDATE`` above anyway — e.g. inserted by a still-running old container,
  during a rolling deploy, in the gap between the ``UPDATE`` and this
  migration's own read of what's left. This half is expected to self-heal:
  the follow-up backfill re-run picks it up.

Both halves are *reported* (pks capped, so a huge count doesn't flood the
deploy log) rather than silently left alone. Only the raced half keeps
counting toward 1RM/PRs in the meantime, via the transitional
``LoggedSet.anchor_slot`` fallback (``models.py``) reading off its
still-live ``prescription`` — the unrecoverable half has no ``prescription``
left to fall back to, so ``.anchored()`` excludes it outright and nothing
keeps it counting.

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


def _partition(rows):
    """Split ``(pk, prescription_id)`` pairs into unrecoverable vs. raced pks.

    A pure function of the one snapshot read the migration takes (see the
    module docstring for why it must be exactly one read, not several) —
    factored out so a plain unit test can pin both buckets without a second
    database connection, which is what an actual raced row would need to
    construct for real (the migration holds the write lock throughout).
    """
    unrecoverable_pks = [pk for pk, prescription_id in rows if prescription_id is None]
    raced_pks = [pk for pk, prescription_id in rows if prescription_id is not None]
    return unrecoverable_pks, raced_pks


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
            # into this SET subquery — Django applies a queryset's default
            # ordering unless it's explicitly cleared, even though nothing
            # about a single ``pk=OuterRef(...)`` lookup can ever need
            # sorting — and makes the emitted SQL carry an INNER JOIN on
            # ``meso_exerciseslot`` (to sort by ``U1."order"``) purely to
            # satisfy that ordering, with nothing to do with the value
            # actually being selected (``exercise_slot_id``).
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
    # between the ``UPDATE`` above and the read below, during a rolling
    # deploy. That row's ``prescription`` is perfectly live — it just missed
    # the ``UPDATE`` sweep — so it is NOT a #577/#581 casualty, and reporting
    # it as one would send an operator chasing a data-loss bug that isn't
    # there. The two are told apart by whether ``prescription`` itself is
    # NULL.
    #
    # Both counts (and both pk lists) come from ONE snapshot read — not three
    # separate queries — so they can never disagree with each other. Three
    # separate reads under READ COMMITTED can each see a different, later
    # state of the table: a concurrent delete between an "unrecoverable"
    # count and a "still NULL" count can make a naively-computed
    # ``raced = still_null - unrecoverable`` negative; a row that loses its
    # ``prescription`` between two such reads can get reported as the
    # self-healing kind when it is actually a permanent casualty; and a
    # delete between a pk list and its own count can leave the count at zero
    # while the pk list is non-empty. One read can't race itself.
    rows = list(
        LoggedSet.objects.using(db_alias)
        .filter(exercise_slot__isnull=True)
        .order_by("pk")
        .values_list("pk", "prescription_id")
    )
    unrecoverable_pks, raced_pks = _partition(rows)
    unrecoverable = len(unrecoverable_pks)
    raced = len(raced_pks)

    if unrecoverable:
        shown_pks = unrecoverable_pks[:20]
        print(
            f"0051_backfill_loggedset_exercise_slot: {unrecoverable} LoggedSet "
            "row(s) have no prescription to backfill exercise_slot from "
            f"(first {len(shown_pks)} pks): {shown_pks}"
        )

    if raced:
        shown_pks = raced_pks[:20]
        print(
            f"0051_backfill_loggedset_exercise_slot: {raced} additional "
            "LoggedSet row(s) still have exercise_slot NULL but a LIVE "
            "prescription — these raced this migration's UPDATE (e.g. a "
            "still-running old container inserted them mid-deploy) rather "
            "than predating it, and the follow-up backfill re-run will pick "
            f"them up (first {len(shown_pks)} pks): {shown_pks}"
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
