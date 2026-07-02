"""Plan-wide snapshot serializer/restorer for the undo/redo op-log (Phase 1).

The designer needs plan-wide undo/redo (``docs/archive/meso/designer-framework-plan.md``
Decision 2 + Phase 1), built on Phase 0's soft delete
(``Week``/``Session``/``ExercisePrescription.deleted_at``). Every mutating
designer endpoint records ONE ``PlanAction`` (see ``models.py``) on the undo
stack, right before its write, via ``record_plan_action``. The endpoints
themselves (``views.api_plan_undo``/``api_plan_redo``) pop/push stack rows and
call ``restore_plan_snapshot`` to apply a popped snapshot back onto the plan.

A snapshot (``serialize_plan_snapshot``) is plan-wide and captures every row
**including soft-deleted ones** — that's how undo of a delete works, and how
redo of an add revives the exact same pk rather than recreating a new row.
Deliberately excluded: ``delivered_at``, ``WeekDelivery``, ``SessionLog``/
``LoggedSet``, ``AthleteOneRm``, and mesocycle fields — undo must never touch
delivery stamps or athlete data.
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

    Soft delete guarantees every ``Week``/``Session``/``ExercisePrescription``
    pk a snapshot ever recorded stays present (never hard-deleted) — this can
    only fire if something bypassed soft delete (e.g. a raw queryset
    ``.delete()``). The undo/redo endpoints turn this into a 409 "History
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

    Captures ALL ``Week``/``Session``/``ExercisePrescription`` rows belonging to
    ``plan`` — including soft-deleted ones — plus every ``PrescriptionOverride``
    on the plan's prescriptions (only ever non-empty for a group plan, since an
    override's ``clean``/``set_override`` only ever target a same-group
    prescription). Field lists mirror what the designer actually edits; delivery
    stamps, logs, and athlete data are never captured (see module docstring).
    """
    weeks = models.Week.objects.filter(mesocycle__plan=plan)
    sessions = models.Session.objects.filter(week__mesocycle__plan=plan)
    prescriptions = models.ExercisePrescription.objects.filter(
        session__week__mesocycle__plan=plan
    )
    overrides = models.PrescriptionOverride.objects.filter(
        prescription__session__week__mesocycle__plan=plan
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
                "is_current": w.is_current,
                "deleted_at": _iso(w.deleted_at),
            }
            for w in weeks
        ],
        "sessions": [
            {
                "pk": s.pk,
                "week_id": s.week_id,
                "day_number": s.day_number,
                "name": s.name,
                "bias": s.bias,
                "order": s.order,
                "deleted_at": _iso(s.deleted_at),
            }
            for s in sessions
        ],
        "prescriptions": [
            {
                "pk": p.pk,
                "session_id": p.session_id,
                "exercise_id": p.exercise_id,
                "name": p.name,
                "order": p.order,
                "sets": p.sets,
                "reps": p.reps,
                "load": p.load,
                "rpe": p.rpe,
                "load_type": p.load_type,
                "note": p.note,
                "tags": list(p.tags or []),
                "deleted_at": _iso(p.deleted_at),
            }
            for p in prescriptions
        ],
        # Natural-keyed (membership_id, prescription_id) — an override has no pk
        # identity worth preserving (``restore_plan_snapshot`` reconciles by this
        # key: create missing, update differing, delete extras). Field names
        # mirror ``GroupMembership.set_override``'s kwargs.
        "overrides": [
            {
                "membership_id": o.membership_id,
                "prescription_id": o.prescription_id,
                "swap": o.swap_name,
                "load_pct": o.load_pct,
                "sets": o.sets,
                "reps": o.reps,
                "note": o.note,
            }
            for o in overrides
        ],
    }


def restore_plan_snapshot(plan, snapshot):
    """Restore ``plan``'s editable rows to ``snapshot``.

    Runs inside the caller's transaction (the caller holds ``select_for_update``
    on the plan). Every snapshot pk must still exist — soft delete guarantees
    this unless something bypassed it, in which case ``HistoryUnavailable`` is
    raised so the caller's transaction rolls back cleanly (nothing is ever
    half-restored). Rows named in the snapshot have every captured field
    (including ``deleted_at``/``is_current``) written back; a plan row **absent**
    from the snapshot (created after it was taken) is soft-deleted — restore
    never hard-deletes or recreates a ``Week``/``Session``/``ExercisePrescription``
    row, so a later redo revives the exact same pk. ``PrescriptionOverride`` rows
    are reconciled to the snapshot by natural key (create missing, update
    differing, delete extras) — hard delete is fine there: no history hangs off
    an override and the natural key makes recreation stable.
    """
    week_rows = {row["pk"]: row for row in snapshot.get("weeks", [])}
    session_rows = {row["pk"]: row for row in snapshot.get("sessions", [])}
    prescription_rows = {row["pk"]: row for row in snapshot.get("prescriptions", [])}

    week_pks = set(week_rows)
    session_pks = set(session_rows)
    prescription_pks = set(prescription_rows)

    existing_week_pks = set(
        models.Week.objects.filter(pk__in=week_pks).values_list("pk", flat=True)
    )
    if existing_week_pks != week_pks:
        raise HistoryUnavailable("A snapshotted week no longer exists.")
    existing_session_pks = set(
        models.Session.objects.filter(pk__in=session_pks).values_list("pk", flat=True)
    )
    if existing_session_pks != session_pks:
        raise HistoryUnavailable("A snapshotted session no longer exists.")
    existing_prescription_pks = set(
        models.ExercisePrescription.objects.filter(pk__in=prescription_pks).values_list(
            "pk", flat=True
        )
    )
    if existing_prescription_pks != prescription_pks:
        raise HistoryUnavailable("A snapshotted prescription no longer exists.")

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

    for session in models.Session.objects.filter(pk__in=session_pks):
        row = session_rows[session.pk]
        session.day_number = row["day_number"]
        session.name = row["name"]
        session.bias = row["bias"]
        session.order = row["order"]
        session.deleted_at = _parse_dt(row["deleted_at"])
        session.save()

    for presc in models.ExercisePrescription.objects.filter(pk__in=prescription_pks):
        row = prescription_rows[presc.pk]
        # ``session_id`` is restored too (Phase 4, #403): before
        # ``prescription_move``, no endpoint ever re-pointed a prescription's
        # session, so this was captured in the snapshot but never needed
        # writing back. A move's undo must put the row back in its source
        # session, not just its old order within whatever session it's in now.
        presc.session_id = row["session_id"]
        presc.exercise_id = row["exercise_id"]
        presc.name = row["name"]
        presc.order = row["order"]
        presc.sets = row["sets"]
        presc.reps = row["reps"]
        presc.load = row["load"]
        presc.rpe = row["rpe"]
        presc.load_type = row["load_type"]
        presc.note = row["note"]
        presc.tags = list(row["tags"] or [])
        presc.deleted_at = _parse_dt(row["deleted_at"])
        presc.save()

    # Rows of this plan created *after* the snapshot was taken are absent from
    # it — soft-delete them (never hard-delete: a later undo of an even-older
    # action must still find the row, and redo must always revive a pk rather
    # than recreate one).
    now = timezone.now()
    models.Week.objects.filter(mesocycle__plan=plan).exclude(pk__in=week_pks).update(
        deleted_at=now
    )
    models.Session.objects.filter(week__mesocycle__plan=plan).exclude(
        pk__in=session_pks
    ).update(deleted_at=now)
    models.ExercisePrescription.objects.filter(
        session__week__mesocycle__plan=plan
    ).exclude(pk__in=prescription_pks).update(deleted_at=now)

    # Reconcile overrides to the snapshot by natural key (membership, prescription).
    # A membership hard-deletes when its athlete leaves the group (taking its
    # override rows with it, CASCADE) — a snapshot recorded before that may
    # still name it. Skip those rows rather than recreate them: the insert
    # would die on the dead FK, and failing the whole restore (409) over a
    # roster change would brick every older undo step. The departed member's
    # adjustments simply stay gone — membership isn't plan-editable state.
    snapshot_overrides = snapshot.get("overrides", [])
    live_membership_ids = set(
        models.GroupMembership.objects.filter(
            pk__in={row["membership_id"] for row in snapshot_overrides}
        ).values_list("pk", flat=True)
    )
    snapshot_by_key = {
        (row["membership_id"], row["prescription_id"]): row
        for row in snapshot_overrides
        if row["membership_id"] in live_membership_ids
    }
    existing_by_key = {
        (o.membership_id, o.prescription_id): o
        for o in models.PrescriptionOverride.objects.filter(
            prescription__session__week__mesocycle__plan=plan
        )
    }
    for key, override in existing_by_key.items():
        if key not in snapshot_by_key:
            override.delete()
    for key, row in snapshot_by_key.items():
        existing = existing_by_key.get(key)
        if existing is not None:
            existing.swap_name = row["swap"]
            existing.load_pct = row["load_pct"]
            existing.sets = row["sets"]
            existing.reps = row["reps"]
            existing.note = row["note"]
            existing.save()
        else:
            models.PrescriptionOverride.objects.create(
                membership_id=key[0],
                prescription_id=key[1],
                swap_name=row["swap"],
                load_pct=row["load_pct"],
                sets=row["sets"],
                reps=row["reps"],
                note=row["note"],
            )


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
