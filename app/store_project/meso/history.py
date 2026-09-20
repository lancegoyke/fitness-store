"""Plan-wide snapshot serializer/restorer for the undo/redo op-log (Phase 1).

The designer needs plan-wide undo/redo (``docs/archive/meso/designer-framework-plan.md``
Decision 2 + Phase 1), built on Phase 0's soft delete
(``Week``/``Session``/``SessionSlot``/``ExerciseSlot.deleted_at``). Every
mutating designer endpoint records ONE ``PlanAction`` (see ``models.py``) on
the undo stack, right before its write, via ``record_plan_action``. The
endpoints themselves (``views.api_plan_undo``/``api_plan_redo``) pop/push
stack rows and call ``restore_plan_snapshot`` to apply a popped snapshot back
onto the plan.

A snapshot (``serialize_plan_snapshot``) is plan-wide and captures every row
**including soft-deleted ones** — that's how undo of a delete works, and how
redo of an add revives the exact same pk rather than recreating a new row.
This is true for ``Week``/``SessionSlot``/``ExerciseSlot``/``Session`` — the
P0 fixed-lineup cutover's ``Prescription`` **cell** has no ``deleted_at`` of
its own (it's live iff its ``ExerciseSlot`` *and* its ``Week`` are both live),
so a cell's row is written back by pk like the others, but a stray one absent
from the snapshot is hard-deleted rather than soft-deleted — see
``restore_plan_snapshot``. Deliberately excluded: ``delivered_at``,
``WeekDelivery``, ``SessionLog``/``LoggedSet``, ``AthleteOneRm``, and
mesocycle fields — undo must never touch delivery stamps or athlete data.
"""

from django.db.models import Exists
from django.db.models import Max
from django.db.models import OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import models

# History cap (Phase 1 spec): keep at most this many rows on a plan's undo
# stack, trimming the oldest (lowest-seq) after each recording. The redo stack
# is bounded naturally (it only ever grows from an undo, and any fresh
# mutation clears it).
UNDO_STACK_CAP = 50


class HistoryUnavailable(Exception):
    """A snapshot references a plan row that no longer exists.

    Soft delete guarantees every ``Week``/``SessionSlot``/``ExerciseSlot``/
    ``Session`` pk a snapshot ever recorded stays present (never hard-deleted)
    — this can only fire (for those rows) if something bypassed soft delete
    (e.g. a raw queryset ``.delete()``). A snapshotted ``Prescription`` cell
    pk going missing does NOT raise this — cells are legitimately hard-deleted
    by ``restore_plan_snapshot``'s own cleanup and recreated by pk on a later
    restore (see there), so a missing cell is an upsert, not an integrity
    error. The undo/redo endpoints turn this exception into a 409 "History
    unavailable" and roll back the whole attempt.
    """


def _iso(dt):
    """A ``datetime`` as an ISO string for JSON storage, or ``None``."""
    return dt.isoformat() if dt else None


def _parse_dt(value):
    """The inverse of ``_iso`` — an ISO string back to a ``datetime``, or ``None``."""
    if value is None:
        return None
    return parse_datetime(value)


def serialize_plan_snapshot(plan):
    """A self-contained, plan-wide snapshot of every editable row.

    Captures ALL ``Week``/``SessionSlot``/``ExerciseSlot``/``Session``/
    ``Prescription`` rows belonging to ``plan`` — including soft-deleted ones.
    Field lists mirror what the designer actually edits; delivery stamps,
    logs, and athlete data are never captured (see module docstring).

    The P0 fixed-lineup cutover split the old per-week ``ExercisePrescription``
    into a fixed ``SessionSlot``(day)/``ExerciseSlot``(row) identity plus a
    per-week ``Prescription`` cell — so this snapshot now captures all four
    row kinds instead of the old three.
    """
    weeks = models.Week.objects.filter(mesocycle__plan=plan)
    session_slots = models.SessionSlot.objects.filter(mesocycle__plan=plan)
    exercise_slots = models.ExerciseSlot.objects.filter(
        session_slot__mesocycle__plan=plan
    )
    sessions = models.Session.objects.filter(week__mesocycle__plan=plan)
    # Athlete-authored cells (Phase 4a) are invisible to the coach's undo/redo:
    # the athlete's own tracking sub-lines record no ``PlanAction`` and must
    # never be reverted by a coach undo, so they're excluded from capture.
    cells = models.Prescription.objects.filter(
        week__mesocycle__plan=plan, athlete_authored=False
    )
    return {
        "weeks": [
            {
                "pk": w.pk,
                "index": w.index,
                "phase": w.phase,
                "volume": w.volume,
                "intensity": w.intensity,
                "is_deload": w.is_deload,
                "deleted_at": _iso(w.deleted_at),
            }
            for w in weeks
        ],
        "session_slots": [
            {
                "pk": s.pk,
                "mesocycle_id": s.mesocycle_id,
                "day_number": s.day_number,
                "name": s.name,
                "bias": s.bias,
                "order": s.order,
                "deleted_at": _iso(s.deleted_at),
            }
            for s in session_slots
        ],
        "exercise_slots": [
            {
                "pk": es.pk,
                "session_slot_id": es.session_slot_id,
                "exercise_id": es.exercise_id,
                "name": es.name,
                "order": es.order,
                "tags": list(es.tags or []),
                "tempo": es.tempo,
                "rest": es.rest,
                "note": es.note,
                "deleted_at": _iso(es.deleted_at),
            }
            for es in exercise_slots
        ],
        "sessions": [
            {
                "pk": s.pk,
                "week_id": s.week_id,
                "session_slot_id": s.session_slot_id,
                "deleted_at": _iso(s.deleted_at),
            }
            for s in sessions
        ],
        "cells": [
            {
                "pk": c.pk,
                "exercise_slot_id": c.exercise_slot_id,
                "week_id": c.week_id,
                "line": c.line,
                "text": c.text,
                "skipped": c.skipped,
            }
            for c in cells
        ],
    }


def _cells_athlete_data_points_at(pks):
    """The subset of ``pks`` some ``LoggedSet`` still names, by any of its three pointers.

    Shared by both halves of ``restore_plan_snapshot``'s "never touch a cell
    athlete data points at" rule — the stray-cell purge at the end of this
    function (#577, #584) and the coordinate-collision guard in the cell
    upsert loop above it (#583). A cell counts as athlete data here when some
    ``LoggedSet`` still names it through ``prescription`` (``logged_sets`` —
    the line-0 cell every logged set is filed under, whatever its origin,
    #577), ``source_line`` (``parsed_sets`` — the sub-line a typed set was
    parsed from, 5a), or ``reclaimed_line`` (``reclaimed_sets`` — #541's hint
    linking a structured "Log session" copy back to the sub-line it replaced).
    ``athlete_authored`` is deliberately NOT folded in here: it is a plain
    field on the cell itself, cheaper for each caller to read directly off an
    instance it already has (or to re-check separately, alongside this call,
    under whatever lock that caller holds) than to add as a fourth branch to
    a query that only exists to chase foreign keys.

    Built as three ``Exists`` subqueries OR'd together, deliberately **not**
    ``.filter(Q(parsed_sets__isnull=False) | Q(reclaimed_sets__isnull=False)
    | Q(logged_sets__isnull=False))``. That form is a three-way LEFT OUTER
    JOIN from ``Prescription`` out to ``LoggedSet``, and a cell with, say, two
    logged sets pointing at it joins to two rows and comes back twice — every
    caller here immediately wraps the result in ``set()``, so the duplicate
    row is harmless in practice, but it is still the wrong shape of query to
    reach for: it does real, avoidable multi-row work (and gets worse the more
    of the three FKs happen to point at the same cell at once) to answer what
    is fundamentally a yes/no question per row. An ``Exists`` correlated
    subquery answers that question directly, one clause per pointer, in a
    single ``WHERE``, with no join and nothing to de-duplicate.
    """
    if not pks:
        return set()
    sets = models.LoggedSet.objects
    return set(
        models.Prescription.objects.filter(pk__in=pks)
        .filter(
            Exists(sets.filter(prescription=OuterRef("pk")))
            | Exists(sets.filter(source_line=OuterRef("pk")))
            | Exists(sets.filter(reclaimed_line=OuterRef("pk")))
        )
        .values_list("pk", flat=True)
    )


def restore_plan_snapshot(plan, snapshot):
    """Restore ``plan``'s editable rows to ``snapshot``.

    Runs inside the caller's transaction (the caller holds ``select_for_update``
    on the plan). Every snapshotted ``Week``/``SessionSlot``/``ExerciseSlot``/
    ``Session`` pk must still exist — soft delete guarantees this unless
    something bypassed it, in which case ``HistoryUnavailable`` is raised so
    the caller's transaction rolls back cleanly (nothing is ever
    half-restored). Rows named in the snapshot have every captured field
    (including ``deleted_at``) written back; a row of one of those four kinds
    **absent** from the snapshot (created after it was taken)
    is soft-deleted — restore never hard-deletes or recreates one of these
    rows, so a later redo revives the exact same pk.

    ``Prescription`` **cells** (the P0 fixed-lineup cutover) are different:
    they carry no ``deleted_at`` of their own — a cell is live iff its
    ``ExerciseSlot`` *and* its ``Week`` are both live, so a cell whose slot or
    week was just soft-deleted above is already hidden without touching the
    cell row itself. Snapshotted cells are UPSERTED by pk (Phase 2a): a
    snapshotted pk gone from the DB is recreated with that exact pk — sub-line
    cells (``line`` >= 1) are created routinely while editing, and undoing
    past one's creation hard-deletes it via the stray-cell cleanup below, so
    redo must revive it verbatim. A cell present in the DB now but *absent*
    from the snapshot is hard-deleted when its ``ExerciseSlot`` **and** its
    ``Week`` are both still live in the snapshot being restored (that's a
    sub-line created after the snapshot — undo removes it; also any bug-made
    stray), which is safe precisely because the pk-upsert makes a later redo
    able to recreate it.
    """
    week_rows = {row["pk"]: row for row in snapshot.get("weeks", [])}
    slot_rows = {row["pk"]: row for row in snapshot.get("session_slots", [])}
    exercise_slot_rows = {row["pk"]: row for row in snapshot.get("exercise_slots", [])}
    session_rows = {row["pk"]: row for row in snapshot.get("sessions", [])}
    cell_rows = {row["pk"]: row for row in snapshot.get("cells", [])}

    week_pks = set(week_rows)
    slot_pks = set(slot_rows)
    exercise_slot_pks = set(exercise_slot_rows)
    session_pks = set(session_rows)
    cell_pks = set(cell_rows)

    existing_week_pks = set(
        models.Week.objects.filter(pk__in=week_pks).values_list("pk", flat=True)
    )
    if existing_week_pks != week_pks:
        raise HistoryUnavailable("A snapshotted week no longer exists.")
    existing_slot_pks = set(
        models.SessionSlot.objects.filter(pk__in=slot_pks).values_list("pk", flat=True)
    )
    if existing_slot_pks != slot_pks:
        raise HistoryUnavailable("A snapshotted session slot no longer exists.")
    existing_exercise_slot_pks = set(
        models.ExerciseSlot.objects.filter(pk__in=exercise_slot_pks).values_list(
            "pk", flat=True
        )
    )
    if existing_exercise_slot_pks != exercise_slot_pks:
        raise HistoryUnavailable("A snapshotted exercise slot no longer exists.")
    existing_session_pks = set(
        models.Session.objects.filter(pk__in=session_pks).values_list("pk", flat=True)
    )
    if existing_session_pks != session_pks:
        raise HistoryUnavailable("A snapshotted session no longer exists.")
    # Cells have no ``deleted_at`` — see the docstring above. A snapshotted
    # cell pk going missing is not an integrity error, so no existence check
    # (and no ``HistoryUnavailable``) here; the write loop below is
    # best-effort over whichever snapshotted pks still exist.

    for week in models.Week.objects.filter(pk__in=week_pks):
        row = week_rows[week.pk]
        week.index = row["index"]
        week.phase = row["phase"]
        week.volume = row["volume"]
        week.intensity = row["intensity"]
        week.is_deload = row["is_deload"]
        week.deleted_at = _parse_dt(row["deleted_at"])
        week.save()

    for slot in models.SessionSlot.objects.filter(pk__in=slot_pks):
        row = slot_rows[slot.pk]
        slot.mesocycle_id = row["mesocycle_id"]
        slot.day_number = row["day_number"]
        slot.name = row["name"]
        slot.bias = row["bias"]
        slot.order = row["order"]
        slot.deleted_at = _parse_dt(row["deleted_at"])
        slot.save()

    for exercise_slot in models.ExerciseSlot.objects.filter(pk__in=exercise_slot_pks):
        row = exercise_slot_rows[exercise_slot.pk]
        exercise_slot.session_slot_id = row["session_slot_id"]
        exercise_slot.exercise_id = row["exercise_id"]
        exercise_slot.name = row["name"]
        exercise_slot.order = row["order"]
        exercise_slot.tags = list(row["tags"] or [])
        exercise_slot.tempo = row.get("tempo", "")
        exercise_slot.rest = row.get("rest", "")
        exercise_slot.note = row.get("note", "")
        exercise_slot.deleted_at = _parse_dt(row["deleted_at"])
        exercise_slot.save()

    for session in models.Session.objects.filter(pk__in=session_pks):
        row = session_rows[session.pk]
        session.week_id = row["week_id"]
        session.session_slot_id = row["session_slot_id"]
        session.deleted_at = _parse_dt(row["deleted_at"])
        session.save()

    # Cells are UPSERTED by pk (Phase 2a): sub-line cells (line >= 1) are
    # created routinely while editing, and an undo taken before one existed
    # hard-deletes it below — so redo must be able to RECREATE the exact pk,
    # not just best-effort skip it (the old behavior, from when cells were
    # only ever created alongside a new slot/week).
    existing_cells = {
        c.pk: c for c in models.Prescription.objects.filter(pk__in=cell_pks)
    }

    # #583 — a snapshotted cell is revived BY PK (that's the whole point of the
    # upsert below: a redo must put back the SAME row a corresponding undo
    # took away, not a lookalike with a new pk), but ``Prescription`` also
    # enforces ``unique_cell_slot_week_line`` on ``(exercise_slot, week,
    # line)`` — and that COORDINATE, not the pk, is a cell's real identity;
    # the pk is only ever an implementation detail of reviving the same row.
    # Nothing stops another writer from putting a *different* row at a
    # coordinate a still-pending redo wants back. Concretely: a coach undo
    # hard-deletes a stray cell (the purge at the end of this function), the
    # athlete then blurs a sub-line at those exact coordinates — their own
    # write path (``athlete_cell_write``) does a plain ``get_or_create``, mints
    # a brand new pk there, and (by design, Phase 4a) records no
    # ``PlanAction``, so nothing tells the redo stack its target coordinate is
    # no longer vacant — and the coach's later redo then tries to ``save()``
    # the OLD pk straight onto a coordinate the new pk already occupies,
    # violating the unique constraint and 500ing the whole redo.
    #
    # Resolve every such collision BEFORE the upsert loop below, coordinate by
    # coordinate, applying the SAME rule the stray-cell purge already states
    # (just one join earlier — to a coordinate a REVIVED pk wants, not only to
    # a stray pk the purge would otherwise delete on its own):
    #
    #   * an occupant that is athlete data — ``athlete_authored``, or a cell
    #     some ``LoggedSet`` still points at (``_cells_athlete_data_points_at``
    #     above) — KEEPS the coordinate. The snapshotted cell that wanted it
    #     is left for the upsert loop below to handle via its existing
    #     same-pk ``athlete_authored`` guard: if the snapshotted pk itself
    #     still exists, that guard `continue`s past it unchanged; if it
    #     doesn't exist (the redo case above), the loop tries to *revive* it
    #     at a coordinate this pass has left occupied, and the same unique
    #     constraint that would have 500ed the whole transaction blocks that
    #     one ``save()`` — so a snapshotted cell must be excluded from the
    #     loop entirely once its coordinate is spared, not merely left alone.
    #   * otherwise the occupant is a coach-made row absent from the snapshot
    #     — exactly what the stray-cell purge below removes on its own, just
    #     discovered here one step earlier because it happens to sit where a
    #     revived pk wants to go — so it is deleted now, freeing the
    #     coordinate for the upsert loop.
    #
    # This delete gets the SAME lock-then-recheck treatment #584 gives the
    # purge, for the identical reason: an unlocked read-then-delete here would
    # reopen, in brand new code, the exact race #584 exists to close (a
    # ``LoggedSet`` or an ``athlete_authored`` flip committed by a concurrent
    # athlete write, between our read and our delete, that we'd otherwise
    # never see). See the purge's own comment below for the full lock
    # reasoning (``of=("self",)``, plain ``FOR UPDATE`` not ``no_key=True``,
    # lock order) — it applies here verbatim, just against a narrower
    # candidate set (occupants of a snapshotted coordinate, not every
    # snapshot-absent cell).
    coord_of_pk = {
        pk: (row["exercise_slot_id"], row["week_id"], row.get("line", 0))
        for pk, row in cell_rows.items()
    }
    # Every coordinate a snapshotted cell could possibly collide with names an
    # exercise slot and a week THIS SAME snapshot also recorded — a cell's
    # ``exercise_slot_id``/``week_id`` was read by ``serialize_plan_snapshot``
    # in the very call that captured ``exercise_slots``/``weeks`` too — so
    # this query, scoped to the plan's own slots/weeks rather than the whole
    # database, is guaranteed to surface every possible occupant.
    occupant_pk_by_coord = {
        (slot_id, week_id, line): occupant_pk
        for slot_id, week_id, line, occupant_pk in models.Prescription.objects.filter(
            exercise_slot_id__in=exercise_slot_pks, week_id__in=week_pks
        ).values_list("exercise_slot_id", "week_id", "line", "pk")
    }
    colliding_pks_to_skip = set()
    stray_candidate_pks = set()
    for pk, coord in coord_of_pk.items():
        occupant_pk = occupant_pk_by_coord.get(coord)
        if occupant_pk is None or occupant_pk == pk:
            continue  # coordinate free, or already correctly occupied by pk itself
        if occupant_pk in cell_pks:
            # The occupant is ANOTHER snapshotted cell — a genuine coordinate
            # SWAP within this one restore (e.g. two rows dragged past each
            # other and back). Resolving that safely would need to move one
            # side off its coordinate onto a temporary, unclaimed spot before
            # either pk could be revived in place — ``unique_cell_slot_week_line``
            # is checked immediately, not deferred, so neither side can simply
            # write into the other's still-occupied coordinate — and no known
            # real path produces this shape today (see
            # ``test_restore_cell_collision.py``'s module docstring). Left
            # AS-IS: such a swap still raises ``IntegrityError`` out of the
            # upsert loop below, exactly as it did before this fix, rather
            # than being silently (and possibly incorrectly) papered over here.
            continue
        stray_candidate_pks.add(occupant_pk)
    if stray_candidate_pks:
        # Lock the candidates, THEN decide what's spared — see the purge's
        # comment below for why the lock alone (not an explicit lock on the
        # writer's side) is what closes the race, and why ``FOR UPDATE`` and
        # not ``no_key=True`` is required.
        locked_stray_pks = list(
            models.Prescription.objects.select_for_update(of=("self",))
            .filter(pk__in=stray_candidate_pks)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        spared_stray_pks = _cells_athlete_data_points_at(locked_stray_pks) | set(
            models.Prescription.objects.filter(
                pk__in=locked_stray_pks, athlete_authored=True
            ).values_list("pk", flat=True)
        )
        doomed_stray_pks = [pk for pk in locked_stray_pks if pk not in spared_stray_pks]
        if doomed_stray_pks:
            models.Prescription.objects.filter(pk__in=doomed_stray_pks).delete()
        # Whichever snapshotted cell wanted a coordinate a SPARED occupant
        # still holds must be excluded from the upsert loop below — sparing
        # only the occupant and then still trying to revive the snapshotted
        # pk onto the same coordinate would hit the same unique constraint
        # this whole block exists to avoid.
        for pk, coord in coord_of_pk.items():
            if occupant_pk_by_coord.get(coord) in spared_stray_pks:
                colliding_pks_to_skip.add(pk)

    for pk, row in cell_rows.items():
        if pk in colliding_pks_to_skip:
            continue
        cell = existing_cells.get(pk) or models.Prescription(pk=pk)
        # Never overwrite an athlete-authored cell (Phase 4a), even when an
        # OLDER snapshot still holds a coach version of that same pk (a
        # capture-only exclusion would let this restore clobber the athlete's
        # later edit). The authority is the CURRENT DB row's flag, not the
        # snapshot's — the snapshot never carries athlete cells at all.
        if cell.athlete_authored:
            continue
        cell.exercise_slot_id = row["exercise_slot_id"]
        cell.week_id = row["week_id"]
        cell.line = row.get("line", 0)
        cell.text = row.get("text", "")
        cell.skipped = row["skipped"]
        cell.save()

    # Rows of this plan created *after* the snapshot was taken are absent from
    # it — soft-delete them (never hard-delete: a later undo of an even-older
    # action must still find the row, and redo must always revive a pk rather
    # than recreate one).
    now = timezone.now()
    models.Week.objects.filter(mesocycle__plan=plan).exclude(pk__in=week_pks).update(
        deleted_at=now
    )
    models.SessionSlot.objects.filter(mesocycle__plan=plan).exclude(
        pk__in=slot_pks
    ).update(deleted_at=now)
    models.ExerciseSlot.objects.filter(session_slot__mesocycle__plan=plan).exclude(
        pk__in=exercise_slot_pks
    ).update(deleted_at=now)
    models.Session.objects.filter(week__mesocycle__plan=plan).exclude(
        pk__in=session_pks
    ).update(deleted_at=now)

    # Cells carry no ``deleted_at`` of their own (see the docstring above): a
    # cell is live iff its ``ExerciseSlot`` *and* its ``Week`` are both live,
    # so a cell whose slot or week was just soft-deleted above is already
    # hidden without touching the cell row. Only hard-delete a cell that's
    # present in the DB now but absent from the snapshot AND whose slot *and*
    # week are BOTH still live in the snapshot — rare (every constructive
    # write path creates a cell alongside whichever of the two is new), but
    # such a stray cell must be purged or it would resurface as a live row
    # the snapshot never accounted for.
    live_exercise_slot_pks_in_snapshot = {
        pk for pk, row in exercise_slot_rows.items() if row["deleted_at"] is None
    }
    live_week_pks_in_snapshot = {
        pk for pk, row in week_rows.items() if row["deleted_at"] is None
    }
    #
    # ``parsed_sets`` joins the same exclusion for a reason the flag misses: a
    # RECLAIMED sub-line is ``athlete_authored=False``, so undoing back past its
    # creation hard-deleted it — and ``LoggedSet.source_line`` is SET_NULL, so
    # the athlete's derived set survived as a source-LESS row. That strips the
    # protection the link carries (the structured logger's replace-delete spares
    # a parsed row it didn't post for, but cannot recognise one whose link is
    # gone), and the next ordinary save destroyed an earned performance. Undo
    # already refuses to touch athlete data; a cell some athlete data POINTS AT
    # is the same promise one join away.
    #
    # ``reclaimed_sets`` (#541) is that same promise for a THIRD kind of
    # pointer: a structured copy a "Log session" left behind still names this
    # cell via ``reclaimed_line``, with no ``source_line`` of its own — a cell
    # a structured copy still answers to is athlete data pointing at it too.
    #
    # ``logged_sets`` (#577) closes the set: ``LoggedSet.prescription`` is the
    # MOST direct of the three pointers — the line-0 cell every logged set is
    # filed under, whatever its origin — and was the one left out. It is also
    # the costliest to lose, because nothing downstream survives it: where a
    # missing ``source_line`` merely strips a row of the protection its link
    # carries, a NULL ``prescription`` drops the set from every derivation that
    # reads it (``personal_records._live_logged_sets``,
    # ``one_rm.derive_one_rm_values``, ``settle`` — all filter
    # ``prescription__isnull=False``). The set stays in the database and stays
    # on the athlete's page as its sub-line's text, while silently ceasing to
    # count toward their estimated 1RM and their records, with nothing to say
    # so. Those filters are right; the cell simply must not be deleted out from
    # under them.
    #
    # Sparing has a visible cost, and it is accepted: a spared cell keeps the
    # text (and ``skipped``) it had when the snapshot was taken WITHOUT it, so
    # for that one cell the undo is a no-op rather than a revert — the restore
    # never rewrites it, since it isn't in ``cell_pks``. That was already true
    # of the two clauses above; with ``logged_sets`` it now reaches the
    # prescription LINE and the em-dash skip, not just freeform sub-lines. It
    # is still the right trade: a coach can retype a line, and nobody can
    # retype the athlete's performance.
    #
    # #584 — qualify under a ROW LOCK, then re-check what's spared AFTER the
    # lock is held, not before. ``QuerySet.delete()`` is SELECT-then-DELETE
    # with no lock of its own: Django's collector runs the qualifying SELECT
    # (the three ``exclude(..._isnull=False)`` clauses above, as NOT EXISTS —
    # see the docstring paragraph above this one) and only then issues
    # ``DELETE ... WHERE id IN (...)``. A ``LoggedSet`` INSERTed and COMMITted
    # in that gap — after the qualifying SELECT, before the DELETE — is
    # invisible to all three clauses, and the cell is deleted out from under
    # it anyway. For ``prescription``/``source_line`` (real FKs) that surfaces
    # as a COMMIT-time deferred constraint violation — a 500 on the coach's
    # undo, rolling back the whole attempt; for ``reclaimed_line``
    # (``db_constraint=False``, #541, deliberately no DB-level FK) nothing
    # stops it at all, and the hint is left silently dangling.
    #
    # ``select_for_update(of=("self",))``. ``of`` is required because this
    # queryset JOINS through ``week__mesocycle__plan`` to scope itself to the
    # plan — without it, Postgres would lock the joined ``Week``/``Mesocycle``/
    # ``Plan`` rows too, which is both unnecessary (the caller already holds
    # the ``Plan`` lock for the whole restore) and a lock-ordering hazard of
    # its own. ``settle.settle_log`` already takes exactly this shape of lock,
    # for the same reason; matched here.
    #
    # Plain ``FOR UPDATE``, deliberately **not** ``no_key=True``. The purpose
    # of this lock is to make the racing write and this delete serialize —
    # concretely, to make sure that if a ``LoggedSet`` insert referencing one
    # of these cells is concurrently in flight, this lock either waits behind
    # it (and the re-check below then sees it and spares the cell) or is
    # granted only once that insert has fully committed (same outcome). A
    # ``LoggedSet`` insert takes a ``FOR KEY SHARE`` lock on the
    # ``Prescription`` row it references, to protect that reference for the
    # rest of its own transaction (Postgres's normal mechanism for enforcing a
    # foreign key under MVCC) — and ``FOR KEY SHARE`` conflicts with
    # ``FOR UPDATE`` but NOT with ``FOR NO KEY UPDATE`` (what
    # ``select_for_update(no_key=True)`` takes). We intend to DELETE these
    # rows if they're still doomed, and a delete needs the stronger lock
    # precisely so it conflicts with that KEY SHARE — using ``no_key=True``
    # here, the fix #560 needed for a plain user-row MUTEX (a lock that was
    # never contending with a concurrent FK reference in the first place),
    # would silently let this lock coexist with the athlete's KEY SHARE
    # instead of waiting on it, defeating the fix in exactly the window it
    # exists to close.
    #
    # ``.order_by("pk")`` gives a deterministic lock-acquisition order among
    # the candidates themselves — irrelevant to any lock this function's
    # caller already holds, but keeps two concurrent restores that both reach
    # this purge from fighting each other over lock order.
    #
    # LOCK ORDER. This stays inside the caller's existing order, unchanged:
    # ``api_plan_undo``/``api_plan_redo`` take ``Plan.objects.select_for_update()``
    # first, then ``restore_plan_snapshot`` writes Week -> SessionSlot ->
    # ExerciseSlot -> Session -> Prescription (the collision guard above, then
    # this purge, last). The plain ``.delete()`` this replaces ALREADY took a
    # lock of ``FOR UPDATE`` strength on these same candidate rows as part of
    # executing its own DELETE statement — issuing a DELETE has always implied
    # locking its targets — so this change only moves *when* that wait happens
    # (before the re-check, instead of implicitly during the DELETE); it
    # introduces no lock-ordering shape that didn't already exist on this path.
    # (#562 is a Session-vs-Plan ordering issue on a DIFFERENT path —
    # ``athlete_cell_write`` vs ``api_plan_undo``/``api_plan_redo`` — and is
    # untouched by this change.)
    #
    # ``athlete_authored`` STAYS in the candidate filter, and is re-checked
    # under the lock as well. Both halves are deliberate.
    #
    # It has to stay in the filter because an athlete-authored cell is never
    # captured in a snapshot (``serialize_plan_snapshot`` excludes them), so
    # it is never in ``cell_pks`` — dropping the exclusion would make EVERY
    # athlete-authored cell in every live slot × live week of the plan a
    # candidate, and this ``FOR UPDATE`` would then lock all of them on every
    # single undo and redo. Those are precisely the rows a logging athlete is
    # writing to, and ``athlete_cell_write`` takes its locks in the opposite
    # order to this path (the sub-line ``Prescription`` via
    # ``cell.save(...)``, THEN ``Plan`` via ``_touch_plan``, while an
    # undo/redo holds ``Plan`` for the whole restore and reaches Prescription
    # last), so locking the athlete's own cells here would turn a rare lock
    # cycle into a routine one — a deadlock generator built by the fix for a
    # different race.
    #
    # Keeping the exclusion costs nothing in correctness, because the flip the
    # re-check exists for is a concurrent UPDATE of one of these very rows
    # (``athlete_cell_write``'s ``cell.save(update_fields=["text",
    # "athlete_authored"])``), and READ COMMITTED already handles that for a
    # locking read: when ``SELECT ... FOR UPDATE`` waits on a row whose
    # updater then commits, PostgreSQL re-evaluates the statement's own WHERE
    # clause against the UPDATED row version and skips it if it no longer
    # matches (the EvalPlanQual recheck, PostgreSQL docs §13.2). So a cell the
    # athlete claims mid-restore drops out of ``candidate_pks`` on its own.
    # The re-check below repeats the test anyway, under the lock, for the same
    # reason ``settle.settle_log`` re-verifies every one of its own
    # conditions rather than trusting the read that selected the row: one
    # place to look for "what spares a cell", evaluated once the row can no
    # longer move.
    #
    # ``select_for_update`` is a documented no-op on SQLite (its
    # ``has_select_for_update`` is ``False`` and the compiler drops the
    # clause), so the whole suite — including every existing test that calls
    # ``restore_plan_snapshot`` directly with no surrounding lock of its own —
    # still runs there unmodified; the lock is only real on Postgres, which is
    # also the only place this race is expressible (see
    # ``test_undo_purge_postgres.py``).
    candidate_pks = list(
        models.Prescription.objects.select_for_update(of=("self",))
        .filter(
            week__mesocycle__plan=plan,
            exercise_slot_id__in=live_exercise_slot_pks_in_snapshot,
            week_id__in=live_week_pks_in_snapshot,
        )
        .exclude(pk__in=cell_pks)
        .exclude(athlete_authored=True)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if candidate_pks:
        # Re-read every spare test NOW, under the lock just acquired. This is
        # the actual fix: a ``LoggedSet`` (or an ``athlete_authored`` flip)
        # committed after some earlier, unlocked read is now guaranteed to be
        # either already visible here, or to have its own writer still
        # blocked behind this lock until this transaction finishes — never
        # silently missed in between, the way the pre-fix plain ``.delete()``
        # could miss it.
        spared_pks = _cells_athlete_data_points_at(candidate_pks) | set(
            models.Prescription.objects.filter(
                pk__in=candidate_pks, athlete_authored=True
            ).values_list("pk", flat=True)
        )
        doomed_pks = [pk for pk in candidate_pks if pk not in spared_pks]
        if doomed_pks:
            models.Prescription.objects.filter(pk__in=doomed_pks).delete()


def record_plan_action(plan, label):
    """Record one UNDO ``PlanAction`` for ``plan``, right before its mutation.

    Must run inside the caller's transaction, called immediately BEFORE the
    write it protects (the snapshot it captures is the pre-mutation state):

    1. Drop the plan's redo stack — a fresh mutation forks history, so whatever
       could have been redone is no longer reachable.
    2. Allocate the next ``seq`` (one past the max over the plan's remaining
       rows — the redo stack is now empty, so this is the max undo ``seq``).
    3. Insert the UNDO row with a snapshot of the plan's current (pre-mutation)
       state.
    4. Trim the undo stack to ``UNDO_STACK_CAP``, dropping the oldest
       (lowest-seq) rows first.

    Row-locks the plan first: overlapping designer autosaves would otherwise
    both read the same max ``seq`` and the loser's insert would 500 on
    ``unique_plan_action_seq``. (The undo/redo endpoints take the same lock,
    so recording also serializes against a concurrent restore.)
    """
    models.Plan.objects.select_for_update().filter(pk=plan.pk).first()
    # Labels often embed a row's free-text name (255 chars allowed) — clamp to
    # the column, or Postgres rejects the insert and the edit itself 500s.
    max_len = models.PlanAction._meta.get_field("label").max_length
    label = label[:max_len]
    models.PlanAction.objects.filter(
        plan=plan, stack=models.PlanAction.Stack.REDO
    ).delete()
    max_seq = (
        models.PlanAction.objects.filter(plan=plan).aggregate(m=Max("seq"))["m"] or 0
    )
    models.PlanAction.objects.create(
        plan=plan,
        stack=models.PlanAction.Stack.UNDO,
        seq=max_seq + 1,
        label=label,
        snapshot=serialize_plan_snapshot(plan),
    )
    undo_pks = list(
        models.PlanAction.objects.filter(plan=plan, stack=models.PlanAction.Stack.UNDO)
        .order_by("-seq")
        .values_list("pk", flat=True)
    )
    if len(undo_pks) > UNDO_STACK_CAP:
        models.PlanAction.objects.filter(pk__in=undo_pks[UNDO_STACK_CAP:]).delete()
