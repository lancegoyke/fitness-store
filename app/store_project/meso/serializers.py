"""Serialize a real ``Plan`` into the shape the Meso designer renders.

The designer (``static/js/meso.js``) keeps three in-memory arrays:

- ``program`` — the current week's sessions, each with its exercise rows;
- ``weeks``   — the current mesocycle's week strip (volume/intensity bars);
- ``phases``  — the macrocycle rail (one entry per mesocycle).

``serialize_plan`` reproduces that shape from the ``Plan → Mesocycle → Week →
Session → ExercisePrescription`` hierarchy so Phase 3 can hydrate the designer
from the database instead of fixtures. Fields the designer derives from *other*
slices — ``last`` (from logged sets) and ``adj`` (from the agent) — are not
emitted here; they arrive with those slices.
"""

from . import models


def serialize_prescription(prescription):
    """One exercise row in a session's grid."""
    data = {
        "id": prescription.pk,
        "name": prescription.name,
        "sets": prescription.sets,
        "reps": prescription.reps,
        "load": prescription.load,
        "rpe": prescription.rpe,
        "note": prescription.note,
    }
    # The designer renders a single `tag`; the model stores a list.
    if prescription.tags:
        data["tag"] = prescription.tags[0]
    return data


def serialize_session(session):
    """One training day (a column in the designer grid)."""
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        "exercises": [serialize_prescription(p) for p in session.prescriptions.all()],
    }


def serialize_week(week):
    """One column in the designer's week strip."""
    return {
        "label": f"Wk {week.index}",
        "phase": week.phase,
        "vol": week.volume,
        "inten": week.intensity,
        "deload": week.is_deload,
        "current": week.is_current,
    }


def serialize_mesocycle(mesocycle, state):
    """One bar in the macrocycle rail."""
    return {
        "name": mesocycle.name,
        "weeks": f"{mesocycle.week_count} wk",
        "state": state,
    }


def _phase_states(mesocycles, current_mesocycle):
    """Map each mesocycle to done/current/next/future by *sequence position*.

    Position, not ``order`` arithmetic: the model enforces unique — not
    contiguous — ``order``, so reordering/deleting blocks can leave gaps. The
    block immediately following the current one is always ``next``.
    """
    current_index = next(
        (
            i
            for i, m in enumerate(mesocycles)
            if current_mesocycle is not None and m.pk == current_mesocycle.pk
        ),
        None,
    )
    states = []
    for i in range(len(mesocycles)):
        if current_index is None or i > current_index + 1:
            states.append("future")
        elif i < current_index:
            states.append("done")
        elif i == current_index:
            states.append("current")
        else:  # i == current_index + 1
            states.append("next")
    return states


def serialize_week_snapshot(week):
    """A self-contained snapshot of a week, for a ``WeekDelivery`` payload.

    Captures the week's meta plus its full session/prescription grid so a later
    delivery can diff against it ("changes since last delivery").
    """
    return {
        "week": {
            "id": week.pk,
            "index": week.index,
            "phase": week.phase,
            "volume": week.volume,
            "intensity": week.intensity,
            "is_deload": week.is_deload,
        },
        "sessions": [
            serialize_session(s)
            for s in week.sessions.prefetch_related("prescriptions")
        ],
    }


def current_week(plan, week=None):
    """The week the designer opens to.

    An explicit ``week`` wins; otherwise the flagged current week, or — failing
    both — the earliest week in the plan.
    """
    if week is not None:
        return week
    weeks = list(
        models.Week.objects.filter(mesocycle__plan=plan)
        .select_related("mesocycle")
        .order_by("mesocycle__order", "index")
    )
    for candidate in weeks:
        if candidate.is_current:
            return candidate
    return weeks[0] if weeks else None


def serialize_plan(plan, week=None):
    """Serialize ``plan`` to the designer's ``program``/``weeks``/``phases`` shape.

    ``week`` optionally pins which week populates ``program``/``weeks``;
    otherwise the flagged current week (or the plan's first) is used.
    """
    open_week = current_week(plan, week)
    current_mesocycle = open_week.mesocycle if open_week else None

    if open_week is not None:
        sessions = open_week.sessions.prefetch_related("prescriptions")
        program = [serialize_session(s) for s in sessions]
        week_strip = [serialize_week(w) for w in current_mesocycle.weeks.all()]
    else:
        program = []
        week_strip = []

    mesocycles = list(plan.mesocycles.all())
    states = _phase_states(mesocycles, current_mesocycle)
    phases = [serialize_mesocycle(m, s) for m, s in zip(mesocycles, states)]

    return {
        "plan": {
            "id": plan.pk,
            "title": plan.title,
            "goal": plan.goal,
            "status": plan.status,
            "unit": plan.unit,
        },
        "program": program,
        "weeks": week_strip,
        "phases": phases,
    }
