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

import logging

from django.db.models import Exists
from django.db.models import Max
from django.db.models import OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import models

logger = logging.getLogger(__name__)

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
    """The subset of ``pks`` some ``LoggedSet`` still names, by any of its three pointers AT A CELL.

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
    # Read WITHOUT a lock, and the upsert loop's own ``athlete_authored``
    # guard below therefore decides from a pre-lock flag. That is unchanged
    # from before #584 and is safe for the same reason the occupancy read
    # below is: an athlete transaction cannot overlap this part of the
    # restore (see the guard's comment). It is called out here because the
    # purge's comment argues for not DEPENDING on that incidental
    # serialization — this one still does.
    existing_cells = {
        c.pk: c for c in models.Prescription.objects.filter(pk__in=cell_pks)
    }

    # Which slots and weeks the snapshot records as LIVE. Both the collision
    # guard immediately below and the stray-cell purge at the end of this
    # function key off these: a cell whose slot or week is soft-deleted in the
    # snapshot is already hidden without touching the cell row, and the purge
    # deliberately leaves it alone. Computed here, once, so the two use the
    # same definition rather than drifting apart.
    live_exercise_slot_pks_in_snapshot = {
        pk for pk, row in exercise_slot_rows.items() if row["deleted_at"] is None
    }
    live_week_pks_in_snapshot = {
        pk for pk, row in week_rows.items() if row["deleted_at"] is None
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
    #     must be excluded from the upsert loop ENTIRELY, not merely left to
    #     that loop's own same-pk ``athlete_authored`` guard, which fires only
    #     when the snapshotted row is itself athlete-authored and so would not
    #     catch this at all. Left in the loop, the snapshotted pk would be
    #     revived onto a coordinate this pass has just decided to leave
    #     occupied, and the same unique constraint that would have 500ed the
    #     whole transaction blocks that one ``save()`` instead.
    #   * otherwise the occupant is a coach-made row absent from the snapshot
    #     — exactly what the stray-cell purge below removes on its own, just
    #     discovered here one step earlier because it happens to sit where a
    #     revived pk wants to go — so it is deleted now, freeing the
    #     coordinate for the upsert loop.
    #
    # An occupant is removed ONLY when the stray-cell purge at the end of this
    # function would itself have removed it — same spare rule, and the same
    # liveness scoping: a cell whose slot or week is soft-deleted in the
    # snapshot is one the purge deliberately leaves standing, so this guard
    # leaves it standing too and skips the snapshotted cell instead. Without
    # that, this block would hard-delete rows the purge protects, which is a
    # rule the two halves must not disagree about.
    #
    # The delete takes the same lock-then-recheck as the purge, and treats
    # ``athlete_authored`` the same way it does — as a FILTER, never as a
    # locked re-check. Here that decision is made twice over: such an occupant
    # is spared from the unlocked read below and so never reaches the locking
    # SELECT at all, and that SELECT carries ``.exclude(athlete_authored=True)``
    # anyway. (See the branch that spares them for why taking ``FOR UPDATE``
    # on one would deadlock against ``cell_line_write``.) The purge keeps a
    # third, redundant re-read of the flag; this guard drops it, because the
    # filter has already answered it. Only the three ``LoggedSet`` pointers
    # are re-read under the lock.
    #
    # What that lock does NOT do here: the occupancy read below is unlocked,
    # so it cannot see a cell another transaction has inserted at one of these
    # coordinates but not yet committed. What keeps that from becoming a
    # unique-constraint violation is a lock this function takes for an
    # unrelated reason — the ``session.save()`` loop above UPDATEs every
    # snapshotted ``Session`` row, and both athlete write paths
    # (``athlete_cell_write``, ``athlete_log_session``) take
    # ``Session.objects.select_for_update()`` as their first statement, so an
    # athlete transaction cannot overlap the part of this restore that
    # follows that loop. (Precisely: the loop locks the SNAPSHOTTED sessions;
    # a session created after the snapshot is locked a little later, by the
    # soft-delete UPDATE below. A cell at a snapshotted coordinate implies a
    # snapshot-era session, so the guard is covered either way.) A writer that
    # holds neither that Session row nor the ``Plan`` row would reopen the
    # window — ``cell_line_write`` already writes a ``Prescription`` before it
    # takes either, though only ever at a coordinate it then keeps.
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
    occupant_by_coord = {
        (slot_id, week_id, line): (occupant_pk, authored)
        for slot_id, week_id, line, occupant_pk, authored in (
            models.Prescription.objects.filter(
                exercise_slot_id__in=exercise_slot_pks, week_id__in=week_pks
            ).values_list(
                "exercise_slot_id", "week_id", "line", "pk", "athlete_authored"
            )
        )
    }
    colliding_pks_to_skip = set()
    stray_candidate_pks = set()
    for pk, coord in coord_of_pk.items():
        occupant = occupant_by_coord.get(coord)
        if occupant is None or occupant[0] == pk:
            continue  # coordinate free, or already correctly occupied by pk itself
        occupant_pk, occupant_is_athlete_authored = occupant
        if occupant_is_athlete_authored:
            # Spared WITHOUT being locked, and the omission is the point. The
            # purge below keeps ``athlete_authored`` in its candidate FILTER
            # precisely so it never takes ``FOR UPDATE`` on a cell the athlete
            # owns, because ``cell_line_write``'s reclaim writes that row
            # (``existing.save(update_fields=["athlete_authored"])``) BEFORE
            # ``record_plan_action`` takes the ``Plan`` lock — Prescription
            # then Plan, the exact inverse of this path. Locking it here would
            # close that cycle on the guard's MAIN path, since #583's headline
            # occupant IS an athlete-authored cell. So this decision is made
            # from the unlocked read above, exactly as the purge makes it from
            # its filter, and only the ``LoggedSet`` pointers are re-checked
            # under the lock.
            colliding_pks_to_skip.add(pk)
            continue
        if occupant_pk in cell_pks:
            # The occupant is ANOTHER snapshotted cell: this restore wants two
            # pks to trade coordinates (a 2-cycle) or to shuffle along a chain
            # (P1 wants what P2 is about to vacate). Neither is resolved here.
            # ``unique_cell_slot_week_line`` is checked immediately, not
            # deferred, so a 2-cycle genuinely needs one side parked on a
            # temporary, unclaimed coordinate first; a chain would only need
            # the upsert loop ordered free-coordinate-first. Both are left
            # alone for the same reason: a snapshotted pk's coordinate is
            # identical in every snapshot that names it, because
            # ``restore_plan_snapshot`` is the ONLY code in the repo that ever
            # assigns a cell's ``(exercise_slot, week, line)`` — every other
            # creation site mints a cell at a coordinate and never moves it.
            # So neither shape is reachable, and both still raise
            # ``IntegrityError`` out of the upsert loop exactly as they did
            # before this fix rather than being papered over on a path no test
            # can exercise. See ``test_restore_cell_collision.py``.
            continue
        if (
            coord[0] not in live_exercise_slot_pks_in_snapshot
            or coord[1] not in live_week_pks_in_snapshot
        ):
            # The purge would spare this occupant (its slot or week is
            # soft-deleted in the snapshot), so this guard spares it too — and
            # therefore must skip the snapshotted cell that wanted its
            # coordinate, or the upsert below would hit the very constraint
            # this block exists to avoid.
            colliding_pks_to_skip.add(pk)
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
            .exclude(athlete_authored=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        spared_stray_pks = _cells_athlete_data_points_at(locked_stray_pks)
        doomed_stray_pks = [pk for pk in locked_stray_pks if pk not in spared_stray_pks]
        if doomed_stray_pks:
            # ``week__mesocycle__plan`` is redundant given where these pks came
            # from, and kept anyway: the pre-change purge carried that scoping
            # in the DELETE's own WHERE, and a delete that can only ever touch
            # this plan's rows should say so at the point it deletes.
            models.Prescription.objects.filter(
                pk__in=doomed_stray_pks, week__mesocycle__plan=plan
            ).delete()
        # Whichever snapshotted cell wanted a coordinate a SPARED occupant
        # still holds must be excluded from the upsert loop below — sparing
        # only the occupant and then still trying to revive the snapshotted
        # pk onto the same coordinate would hit the same unique constraint
        # this whole block exists to avoid.
        for pk, coord in coord_of_pk.items():
            occupant = occupant_by_coord.get(coord)
            if occupant is not None and occupant[0] in spared_stray_pks:
                colliding_pks_to_skip.add(pk)

    for pk, row in cell_rows.items():
        if pk in colliding_pks_to_skip:
            # A skip is otherwise a silent outcome: the endpoint answers
            # ``ok: true`` and that one line simply does not come back. Log
            # both pks so "my redo lost a line" is answerable afterwards.
            #
            # The message deliberately says only that the coordinate is taken,
            # not WHY. Three different branches above land here — an
            # athlete-authored occupant, an occupant under a slot or week this
            # snapshot has soft-deleted, and an occupant some ``LoggedSet``
            # names — and only the first and third involve athlete data at
            # all. Nor is a skip necessarily permanent: a later restore whose
            # snapshot has that slot and week live takes the stray-delete
            # branch instead, and a coach reclaim through ``cell_line_write``
            # flips an athlete-authored occupant back to coach-owned, after
            # which it is deletable again. An earlier draft of this comment
            # claimed both the reason and the permanence, and was wrong on
            # each.
            logger.info(
                "meso.history: skipped restoring cell %s at %s — "
                "coordinate held by cell %s",
                pk,
                coord_of_pk.get(pk),
                (occupant_by_coord.get(coord_of_pk.get(pk)) or (None,))[0],
            )
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
    # filed under, whatever its origin — and was the one left out.
    #
    # #578 C1 changed what's actually at stake here, so the claim this
    # comment used to make is no longer true and is corrected rather than
    # left to rot: a NULL ``prescription`` no longer silently drops a set
    # from every derivation that reads it. ``personal_records.
    # _live_logged_sets``, ``one_rm.derive_one_rm_values``, and ``settle``
    # now read ``LoggedSet.exercise_slot`` (with a transitional fallback to
    # ``prescription.exercise_slot`` — see ``LoggedSet.anchor_slot``), and
    # ``exercise_slot`` is a separate FK straight to the ``ExerciseSlot``,
    # untouched by hard-deleting the ``Prescription`` cell this purge is
    # about — so the set keeps counting toward 1RM/PRs either way.
    #
    # The guard stays in the OR anyway. Sparing has a real, visible cost (see
    # below), and it is accepted — worth paying rather than free — and the
    # OTHER two pointers still carry protection the TYPED path actually
    # depends on: losing ``source_line`` breaks how a re-blur of a sub-line
    # finds and replaces its own derived row rather than minting a twin
    # (a *present* ``source_line`` is what the lookup keys on), and a missing
    # ``reclaimed_line`` loses #541's hint linking a structured "Log session"
    # copy back to the sub-line it replaced. Losing either is a live bug in
    # the write path itself, wholly apart from whether the set still counts —
    # so this clause is defense in depth for ``logged_sets`` now, not the
    # load-bearing one it was before C1, but there is no reason to narrow the
    # OR just because one of its three reasons got weaker.
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
    # WHAT ACTUALLY SERIALIZES AN ATHLETE WRITE TODAY — and it is not this
    # lock, so do not let this comment imply otherwise. An inserted
    # ``LoggedSet`` takes NO lock at all on the ``Prescription`` it
    # references while its transaction runs: Django emits these FKs
    # ``DEFERRABLE INITIALLY DEFERRED`` on Postgres (confirmed against the
    # live schema), so the constraint's own ``FOR KEY SHARE`` on the parent
    # row fires only at COMMIT. The thing that keeps a racing athlete write
    # and this purge apart is the ``session.save()`` loop earlier in this
    # function: it UPDATEs every snapshotted ``Session`` row, and BOTH athlete
    # write paths (``athlete_cell_write``, ``athlete_log_session``) take
    # ``Session.objects.select_for_update()`` as their first statement — so an
    # athlete transaction either commits entirely before this restore reaches
    # that loop, or cannot start until this restore has committed. Verified:
    # giving the regression test's athlete thread that Session lock makes the
    # cell survive on ``main`` too, for a writer that updates the cell AND for
    # the ``athlete_log_session`` shape that never touches it.
    #
    # WHAT THIS LOCK ADDS, then, is that the guarantee stops depending on an
    # incidental UPDATE in an unrelated earlier loop. Make that Session save
    # conditional (skip unchanged rows — a plausible optimization) and the
    # race reopens with nothing to catch it; hold the lock here and the purge
    # states its own requirement locally. It also covers a writer that touches
    # one of these cells without holding the plan's Session rows: such a write
    # takes Postgres's implicit row lock on the cell, this lock waits on it,
    # and the re-check below then sees whatever it committed. See
    # ``test_undo_purge_postgres.py``, which pins both halves separately.
    #
    # Plain ``FOR UPDATE``, deliberately **not** ``no_key=True``, for two
    # reasons. We intend to DELETE these rows, and a DELETE takes a lock of
    # that strength anyway — acquiring a weaker one first would only mean
    # upgrading mid-statement. And ``FOR UPDATE`` is what conflicts with the
    # commit-time ``FOR KEY SHARE`` above, so a writer already INSIDE its
    # COMMIT makes this lock wait, and the re-check then sees its row;
    # ``FOR NO KEY UPDATE`` (what ``no_key=True`` takes) does not conflict
    # with ``FOR KEY SHARE`` and would let the delete proceed alongside it.
    # #560's ``no_key=True`` was the right call for a plain user-row MUTEX
    # that was never contending with an FK reference; this is the opposite
    # case.
    #
    # ``.order_by("pk")`` is plain defensiveness, not a fix for anything
    # reachable: two restores of one plan already serialize on the ``Plan``
    # lock, and restores of different plans have disjoint candidates. It costs
    # nothing and means the acquisition order is stated rather than incidental.
    #
    # LOCK ORDER. The sequence is unchanged: ``api_plan_undo``/``api_plan_redo``
    # take ``Plan.objects.select_for_update()`` first, then this function
    # writes Week -> SessionSlot -> ExerciseSlot -> Session -> Prescription
    # (the collision guard above, then this purge, last). What DID change is
    # the SIZE of the locked set, and it is worth being exact rather than
    # claiming nothing moved: the ``.delete()`` this replaces carried its
    # three spare clauses inside its own qualifying SELECT, so it only ever
    # locked the DOOMED rows, whereas this locks every snapshot-absent,
    # non-athlete-authored stray — including ones a ``LoggedSet`` names, which
    # are then spared. Those extra rows can include a reclaimed sub-line an
    # athlete is actively blurring, and ``athlete_cell_write`` takes
    # Prescription before ``Plan`` (``cell.save`` then ``_touch_plan``) while
    # this path holds ``Plan`` and wants Prescription. That is a widening of a
    # cycle that already existed on this path, not a new one — the same
    # endpoint pair already inverts on Session-vs-Plan (#562) — and keeping
    # ``athlete_authored`` in the candidate filter below is what stops the
    # widening from being far larger. #562 itself is untouched here, and its
    # direction (pick one Session/Plan order) stays compatible either way.
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
    #
    # Which makes the ``athlete_authored`` half of the re-check below dead on
    # Postgres — it can only ever return zero rows there, and that is said
    # here rather than left for the next reader to work out. It is kept for
    # one reason: "what spares a cell" is then answered in exactly one place,
    # evaluated once the rows can no longer move, the same way
    # ``settle.settle_log`` re-verifies every one of its own conditions
    # instead of trusting the read that selected the row. It is the only
    # check evaluated AFTER the rows could no longer move — the candidate
    # filter still runs on every backend, including the ones where
    # ``select_for_update`` is a no-op (SQLite).
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
            # Plan-scoped at the point of deletion, as the single-statement
            # delete this replaces was — see the collision guard's note above.
            models.Prescription.objects.filter(
                pk__in=doomed_pks, week__mesocycle__plan=plan
            ).delete()


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
