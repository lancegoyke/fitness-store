"""Write approved agent proposals back into the program (agent slice Phase 2).

The review screen is the human gate; once a coach approves changes, this module
performs the structured edit each ``ProposedChange`` describes:

- **swap**     → rename the block-shared ``ExerciseSlot`` (every week follows);
- **progress** → rewrite the cell's freeform text with the new load;
- **volume**   → rewrite the cell's freeform text with the new set count;
- **deload**   → flag the target week (``is_deload``);
- **add**      → create a new exercise row on the target session (the draft verb).

Text-first (Phase 2a): a cell is one freeform ``text`` string, so progress/
volume applies parse the current text (``parsing.parse_prescription``), swap
in the proposed component, and recompose (``compose_prescription_text``). A
cell whose text the parser can't recover structure from is a safe skip, never
a blind overwrite of the coach's notation.

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
from ..parsing import compose_prescription_text
from ..parsing import parse_prescription
from ..serializers import first_live_week


def _parsed_bits(cell):
    """The cell's parsed (sets, reps, rpe, load) as compose-ready strings.

    Returns ``None`` when the text carries no recoverable sets/reps structure
    at all (a packed circuit, prose) — callers treat that as "don't touch the
    coach's notation".
    """
    parsed = parse_prescription(cell.text) or {}
    reps = parsed.get("reps")
    if reps is None and parsed.get("reps_range"):
        low, high = parsed["reps_range"]
        reps = f"{low}-{high}"
    if reps is None and parsed.get("duration"):
        reps = parsed["duration"]
    if reps is not None and parsed.get("unit"):
        reps = f"{reps} {parsed['unit']}"
    sets = parsed.get("sets")
    if cell.text.strip() and sets is None and reps is None:
        return None
    return {
        "sets": "" if sets is None else str(sets),
        "reps": "" if reps is None else str(reps),
        "rpe": parsed.get("rpe") or "",
        "load": parsed.get("load") or "",
    }


def _rewrite_cell(change, component, value):
    """Recompose the cell's text with one component (``sets``/``load``) replaced.

    The text-first analogue of the old per-field write: parse the current
    freeform text, substitute the proposed component, and recompose in
    canonical notation. A cell whose non-empty text yields no structure is a
    safe skip (``None``) — never overwrite notation the parser can't read.
    """
    presc = change.prescription
    if presc is None or not value:
        return None
    bits = _parsed_bits(presc)
    if bits is None:
        return None
    bits[component] = str(value)
    presc.text = compose_prescription_text(**bits)
    presc.save(update_fields=["text"])
    return {"id": change.pk, "kind": change.kind, "field": component, "value": value}


def _apply_volume(change):
    """Set the new set count (recomposed into the cell's text).

    A volume change may target one exercise row (set that row) or a whole day
    (set every row in the session) — the validation contract allows either. We
    handle both so a valid session-scoped proposal is never a silent no-op.
    """
    sets = (change.payload or {}).get("sets")
    if not sets:
        return None
    if change.prescription is not None:
        return _rewrite_cell(change, "sets", sets)
    if change.session_id is None:
        return None
    cells = list(change.session.cells())
    rewritten = 0
    for cell in cells:
        bits = _parsed_bits(cell)
        if bits is None:
            continue
        bits["sets"] = str(sets)
        cell.text = compose_prescription_text(**bits)
        cell.save(update_fields=["text"])
        rewritten += 1
    if not rewritten:
        return None
    return {
        "id": change.pk,
        "kind": change.kind,
        "field": "sets",
        "value": sets,
        "count": rewritten,
    }


def _apply_add(change):
    """Create a new exercise row on the change's target session (the draft verb).

    An ``add`` has no prescription to edit — it appends a brand-new row to the
    day. Since the fixed-lineup cutover, a "row" is an ``ExerciseSlot`` shared
    across the whole block, so creating one must keep the dense-grid invariant:
    a ``Prescription`` cell is created for EVERY live week of the mesocycle, not
    just the change's target week. The payload's numbers (sets/reps/load/rpe)
    compose into the target week's cell text only; every other week's cell
    starts blank, same as a coach adding a row by hand. A missing session or
    name is a safe no-op (reported as skipped), mirroring the other kinds.
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
                    text=compose_prescription_text(
                        sets=payload.get("sets", ""),
                        reps=payload.get("reps", ""),
                        rpe=payload.get("rpe", ""),
                        load=payload.get("load", ""),
                    ),
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


def _apply_deload(change):
    """Flag the change's week as a deload (its session's week, else the batch's block).

    A deload targets a whole week, not a specific row, so a candidate that
    named no session (a plan-level "make this a deload week" edit) needs a
    fallback week to flag. That fallback is the batch's *persisted* block's
    earliest live week (``AgentProposalBatch.mesocycle``, §4b) — never a fresh
    re-derivation, and never the plan's earliest-live week regardless of block.
    Degrades to a safe no-op, not a wrong-block write, when the batch carries no
    block (``mesocycle`` is ``None``: no block was ever resolved, or it was
    hard-deleted after the run started — ``SET_NULL``) or that block has no
    live week to flag.
    """
    if change.session_id:
        week = change.session.week
    else:
        week = first_live_week(change.batch.mesocycle)
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
    substitute stays the coach's manual move (typed into a freeform sub-line,
    Phase 2a), not an agent verb. A free-text rename severs the slot's catalog
    link so the row isn't mis-keyed to the old exercise.
    """
    presc = change.prescription
    if presc is None or not name:
        return None
    slot = presc.exercise_slot
    slot.name = name
    slot.exercise = None
    slot.save(update_fields=["name", "exercise"])
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
        return _rewrite_cell(change, "load", payload.get("load"))
    if change.kind == ProposedChange.Kind.VOLUME:
        return _apply_volume(change)
    if change.kind == ProposedChange.Kind.DELOAD:
        return _apply_deload(change)
    if change.kind == ProposedChange.Kind.ADD:
        return _apply_add(change)
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
        ).select_related("prescription", "session__week", "batch__mesocycle")
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
