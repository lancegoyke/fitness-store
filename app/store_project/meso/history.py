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

from django.db.models import Max
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
    cells = models.Prescription.objects.filter(week__mesocycle__plan=plan)
    return {
        "weeks": [
            {
                "pk": w.pk,
                "index": w.index,
                "phase": w.phase,
                "volume": w.volume,
                "intensity": w.intensity,
                "is_deload": w.is_deload,
                "is_current": w.is_current,
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


def restore_plan_snapshot(plan, snapshot):
    """Restore ``plan``'s editable rows to ``snapshot``.

    Runs inside the caller's transaction (the caller holds ``select_for_update``
    on the plan). Every snapshotted ``Week``/``SessionSlot``/``ExerciseSlot``/
    ``Session`` pk must still exist — soft delete guarantees this unless
    something bypassed it, in which case ``HistoryUnavailable`` is raised so
    the caller's transaction rolls back cleanly (nothing is ever
    half-restored). Rows named in the snapshot have every captured field
    (including ``deleted_at``/``is_current``) written back; a row of one of
    those four kinds **absent** from the snapshot (created after it was taken)
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
        week.is_current = row["is_current"]
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
    for pk, row in cell_rows.items():
        cell = existing_cells.get(pk) or models.Prescription(pk=pk)
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
    models.Prescription.objects.filter(
        week__mesocycle__plan=plan,
        exercise_slot_id__in=live_exercise_slot_pks_in_snapshot,
        week_id__in=live_week_pks_in_snapshot,
    ).exclude(pk__in=cell_pks).delete()


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
