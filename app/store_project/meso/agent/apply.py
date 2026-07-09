"""Write approved agent proposals back into the program (agent slice Phase 2).

The review screen is the human gate; once a coach approves changes, this module
performs the structured edit each ``ProposedChange`` describes:

- **swap**     → rename the block-shared ``ExerciseSlot`` (every week follows);
- **progress** → set the prescription's ``load``;
- **volume**   → set the prescription's set count (``sets``);
- **deload**   → flag the target week (``is_deload``);
- **add**      → create a new exercise row on the target session (the draft verb).

The edit value lives in ``ProposedChange.payload`` (built deterministically by
``agent.validation``). ``apply_batch`` runs every non-rejected change in a single
transaction, marks the batch applied, and bumps the plan's ``modified`` so it
reads as the coach's working plan — mirroring the autosave endpoints. A change
that lacks what it needs to apply (e.g. a swap with no target) is a safe no-op
and is reported as skipped, never an error.
"""

from django.db import transaction

from ..models import AgentProposalBatch
from ..models import ExerciseSlot
from ..models import Prescription
from ..models import ProposedChange
from ..serializers import current_week


def _apply_prescription_field(change, field, value):
    """Write ``value`` onto ``field`` of the change's prescription cell.

    Only for REAL per-week cell fields (``load``/``sets``/``reps``/``rpe``/
    ``rest``/``note``) — ``name`` is a read-only resolving property now, so it
    must never be passed here (a swap writes the cell's ``swap_*`` fields via
    ``_apply_swap`` instead).
    """
    presc = change.prescription
    if presc is None or not value:
        return None
    setattr(presc, field, value)
    presc.save(update_fields=[field])
    return {"id": change.pk, "kind": change.kind, "field": field, "value": value}


def _apply_volume(change):
    """Set the new set count.

    A volume change may target one exercise row (set that row) or a whole day
    (set every row in the session) — the validation contract allows either. We
    handle both so a valid session-scoped proposal is never a silent no-op.
    """
    sets = (change.payload or {}).get("sets")
    if not sets:
        return None
    if change.prescription is not None:
        return _apply_prescription_field(change, "sets", sets)
    if change.session_id is None:
        return None
    cells = list(change.session.cells())
    if not cells:
        return None
    for cell in cells:
        cell.sets = sets
        cell.save(update_fields=["sets"])
    return {
        "id": change.pk,
        "kind": change.kind,
        "field": "sets",
        "value": sets,
        "count": len(cells),
    }


def _apply_add(change):
    """Create a new exercise row on the change's target session (the draft verb).

    An ``add`` has no prescription to edit — it appends a brand-new row to the
    day. Since the fixed-lineup cutover, a "row" is an ``ExerciseSlot`` shared
    across the whole block, so creating one must keep the dense-grid invariant:
    a ``Prescription`` cell is created for EVERY live week of the mesocycle, not
    just the change's target week. The payload's numbers (sets/reps/load/rpe)
    land on the target week's cell only; every other week's cell starts blank,
    same as a coach adding a row by hand. A missing session or name is a safe
    no-op (reported as skipped), mirroring the other kinds.
    """
    payload = change.payload or {}
    name = payload.get("name")
    session = change.session
    if session is None or not name:
        return None
    session_slot = session.session_slot
    last = (
        session_slot.exercise_slots.filter(deleted_at__isnull=True)
        .order_by("-order")
        .first()
    )
    order = (last.order + 1) if last is not None else 0
    exercise_slot = ExerciseSlot.objects.create(
        session_slot=session_slot,
        name=name,
        order=order,
    )
    cells = []
    for week in session_slot.mesocycle.weeks.filter(deleted_at__isnull=True):
        if week.pk == session.week_id:
            cells.append(
                Prescription(
                    exercise_slot=exercise_slot,
                    week=week,
                    sets=payload.get("sets", ""),
                    reps=payload.get("reps", ""),
                    load=payload.get("load", ""),
                    rpe=payload.get("rpe", ""),
                )
            )
        else:
            cells.append(Prescription(exercise_slot=exercise_slot, week=week))
    if cells:
        Prescription.objects.bulk_create(cells)
    return {
        "id": change.pk,
        "kind": change.kind,
        "field": "added",
        "value": exercise_slot.pk,
    }


def _apply_adjust(change):
    """Set one member's per-athlete override (groups agent Phase 2).

    Unlike every other kind (which edits the shared row), an ``adjust`` diverges
    *one* member: ``GroupMembership.set_override`` upserts the override the
    designer overlay renders and delivery resolves. A null membership/prescription
    (a member removed between propose and apply) or an empty diff is a safe no-op
    skip, mirroring the other kinds.

    An agent adjust is a *partial* edit (the model proposes only the field(s) it
    wants to change — validation stores just those in ``payload``). ``set_override``
    is a full replace, so we **merge** the proposal onto the member's existing
    override first: a load-only adjust applied to a member who already has a
    coach-authored swap keeps the swap (the coach reviewed a load tweak, not a
    removal). Only the fields the agent actually proposed move.
    """
    membership = change.membership
    presc = change.prescription
    if membership is None or presc is None:
        return None
    payload = change.payload or {}
    existing = membership.overrides.filter(prescription=presc).first()
    merged = {
        "swap_name": existing.swap_name if existing else "",
        "load_pct": existing.load_pct if existing else None,
        "sets": existing.sets if existing else "",
        "reps": existing.reps if existing else "",
        "note": existing.note if existing else "",
    }
    # Overlay only the fields the proposal carried (validation kept just those),
    # leaving the member's other existing adjustments untouched.
    for key in merged:
        if key in payload:
            merged[key] = payload[key]
    override = membership.set_override(presc, **merged)
    if override is None:
        # An empty diff cleared instead of stored — validation should prevent
        # this, but treat it as a skip rather than counting a phantom apply.
        return None
    return {
        "id": change.pk,
        "kind": change.kind,
        "field": "override",
        "value": membership.pk,
    }


def _apply_deload(change):
    """Flag the change's week as a deload (its session's week, else current)."""
    week = change.session.week if change.session_id else current_week(change.batch.plan)
    if week is None:
        return None
    if not week.is_deload:
        week.is_deload = True
        week.save(update_fields=["is_deload"])
    return {"id": change.pk, "kind": change.kind, "field": "is_deload", "value": True}


def _apply_swap(change, name):
    """Rename the exercise for the WHOLE block (P4 structural rescope).

    Under fixed selection the lineup is shared across every week, so an agent
    swap changes identity on the block-shared ``ExerciseSlot`` — every week's
    cell follows (``Prescription.name`` resolves to the slot). A one-week-only
    substitute stays the coach's manual exception (a cell ``swap_*``), not an
    agent verb. A free-text rename severs the slot's catalog link so the row
    isn't mis-keyed to the old exercise.
    """
    presc = change.prescription
    if presc is None or not name:
        return None
    slot = presc.exercise_slot
    slot.name = name
    slot.exercise = None
    slot.save(update_fields=["name", "exercise"])
    # The block grounding serializes each cell's *effective* name, so the model
    # can target a cell that already carries a one-week ``swap_*`` exception. That
    # override shadows the slot's name, so without clearing it the reviewed week
    # would silently keep the old lift while every other week changed. Clear the
    # target cell's exception so the swap the coach approved shows through here
    # too; sibling weeks' own exceptions are left untouched (still block-wide).
    if presc.swap_name or presc.swap_exercise_id:
        presc.swap_name = ""
        presc.swap_exercise = None
        presc.save(update_fields=["swap_name", "swap_exercise"])
    return {"id": change.pk, "kind": change.kind, "field": "name", "value": name}


def apply_change(change):
    """Apply one change to the program. Returns a describing dict, or None (no-op)."""
    payload = change.payload or {}
    if change.kind == ProposedChange.Kind.SWAP:
        # A swap may carry its new name in the payload or only in the
        # contraindication-checked introduces_exercise field.
        name = payload.get("name") or change.introduces_exercise
        return _apply_swap(change, name)
    if change.kind == ProposedChange.Kind.PROGRESS:
        return _apply_prescription_field(change, "load", payload.get("load"))
    if change.kind == ProposedChange.Kind.VOLUME:
        return _apply_volume(change)
    if change.kind == ProposedChange.Kind.DELOAD:
        return _apply_deload(change)
    if change.kind == ProposedChange.Kind.ADD:
        return _apply_add(change)
    if change.kind == ProposedChange.Kind.ADJUST:
        return _apply_adjust(change)
    return None


def apply_batch(batch):
    """Apply every non-rejected change in ``batch`` and mark the batch applied.

    Returns ``{"applied": <count>, "skipped": <count>}``. Rejected changes are
    left untouched; applied changes are stamped ``approved`` for a clean audit
    trail. Idempotency (only-when-pending) is enforced by the caller.
    """
    applied = []
    skipped = 0
    with transaction.atomic():
        changes = batch.changes.exclude(
            status=ProposedChange.Status.REJECTED
        ).select_related(
            "prescription", "session__week", "membership__relationship__athlete"
        )
        for change in changes:
            result = apply_change(change)
            if result is None:
                skipped += 1
                continue
            if change.status != ProposedChange.Status.APPROVED:
                change.status = ProposedChange.Status.APPROVED
                change.save(update_fields=["status"])
            applied.append(result)
        batch.status = AgentProposalBatch.Status.APPLIED
        batch.save(update_fields=["status"])
        # Bump the plan so it reads as the coach's working plan (auto_now).
        batch.plan.save(update_fields=["modified"])
    return {"applied": len(applied), "skipped": skipped}


def dismiss_batch(batch):
    """Discard a batch without applying anything."""
    batch.status = AgentProposalBatch.Status.DISMISSED
    batch.save(update_fields=["status"])
    return batch
