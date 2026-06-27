"""Write approved agent proposals back into the program (agent slice Phase 2).

The review screen is the human gate; once a coach approves changes, this module
performs the structured edit each ``ProposedChange`` describes:

- **swap**     → set the prescription's ``name`` to the introduced exercise;
- **progress** → set the prescription's ``load``;
- **volume**   → set the prescription's set count (``sets``);
- **deload**   → flag the target week (``is_deload``).

The edit value lives in ``ProposedChange.payload`` (built deterministically by
``agent.validation``). ``apply_batch`` runs every non-rejected change in a single
transaction, marks the batch applied, and bumps the plan's ``modified`` so it
reads as the coach's working plan — mirroring the autosave endpoints. A change
that lacks what it needs to apply (e.g. a swap with no target) is a safe no-op
and is reported as skipped, never an error.
"""

from django.db import transaction

from ..models import AgentProposalBatch
from ..models import ProposedChange
from ..serializers import current_week


def _apply_prescription_field(change, field, value):
    """Write ``value`` onto ``field`` of the change's prescription."""
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
    prescriptions = list(change.session.prescriptions.all())
    if not prescriptions:
        return None
    for presc in prescriptions:
        presc.sets = sets
        presc.save(update_fields=["sets"])
    return {
        "id": change.pk,
        "kind": change.kind,
        "field": "sets",
        "value": sets,
        "count": len(prescriptions),
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


def apply_change(change):
    """Apply one change to the program. Returns a describing dict, or None (no-op)."""
    payload = change.payload or {}
    if change.kind == ProposedChange.Kind.SWAP:
        # A swap may carry its new name in the payload or only in the
        # contraindication-checked introduces_exercise field.
        name = payload.get("name") or change.introduces_exercise
        return _apply_prescription_field(change, "name", name)
    if change.kind == ProposedChange.Kind.PROGRESS:
        return _apply_prescription_field(change, "load", payload.get("load"))
    if change.kind == ProposedChange.Kind.VOLUME:
        return _apply_volume(change)
    if change.kind == ProposedChange.Kind.DELOAD:
        return _apply_deload(change)
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
        ).select_related("prescription", "session__week")
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
