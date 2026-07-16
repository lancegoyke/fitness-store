"""Shared fixture builders for the P0 fixed-lineup schema.

The retired ``ExercisePrescriptionFactory(session=..., name=..., sets=...)`` had a
1:1 kwarg shape these helpers preserve, so an old fixture ports with a rename:

    ExercisePrescriptionFactory(session=s, name="Squat", sets="5")  ->  presc(s, name="Squat", sets="5")
    SessionFactory(week=w, day_number=1, name="Lower")              ->  day(w, day_number=1, name="Lower")

Identity (name/order/exercise/tags) now lives on the block-shared ``ExerciseSlot``
row; per-week numbers live on the ``Prescription`` cell. ``presc`` builds both from
a Session (the day) so the cell lands on that day's slot and the session's week.
A Prescription cell has NO ``.session`` and NO ``.deleted_at`` — use ``cell.week``
and soft-delete the slot (``cell.exercise_slot.soft_delete()``) instead.
"""

import itertools

from ..models import ExerciseSlot
from ..models import Prescription
from ..models import Session
from ..models import SessionSlot
from ..parsing import compose_prescription_text

# Mimic the old factories' Sequence defaults so bare calls make distinct rows/days.
_name_seq = itertools.count(1)
_order_seq = itertools.count(0)
_day_seq = itertools.count(1)


def day(week, *, day_number=None, name="", bias="", order=None, session_slot=None):
    """A thin ``Session`` for ``week`` (its block-shared ``SessionSlot`` auto-made).

    The day's identity lives on the ``SessionSlot``, created on the week's
    mesocycle unless one is passed.
    """
    if session_slot is None:
        if day_number is None:
            day_number = next(_day_seq)
        session_slot = SessionSlot.objects.create(
            mesocycle=week.mesocycle,
            day_number=day_number,
            name=name,
            bias=bias,
            order=order if order is not None else (day_number - 1),
        )
    return Session.objects.create(week=week, session_slot=session_slot)


def make_slot(
    session=None, *, session_slot=None, name=None, order=None, exercise=None, tags=None
):
    """A block-shared ``ExerciseSlot`` (row identity) on a day's slot."""
    if session_slot is None:
        session_slot = session.session_slot
    return ExerciseSlot.objects.create(
        session_slot=session_slot,
        name=name if name is not None else f"Exercise {next(_name_seq)}",
        order=order if order is not None else next(_order_seq),
        exercise=exercise,
        tags=list(tags or []),
    )


def presc(
    session=None,
    *,
    name=None,
    order=None,
    exercise=None,
    tags=None,
    exercise_slot=None,
    week=None,
    text=None,
    sets="3",
    reps="10",
    load="60",
    rpe="7",
    rest="",
    tempo="",
    note="",
    skipped=False,
):
    """A line-0 ``Prescription`` cell (+ its ``ExerciseSlot``), text-first.

    Pass a ``Session`` (old style): the row lands on its slot, the cell on its
    week. Or pass ``exercise_slot=`` and ``week=`` explicitly. Returns the cell.

    Phase 2a compat: the old structured kwargs (``sets``/``reps``/``load``/
    ``rpe``) still work — they compose into the cell's freeform ``text``
    (``"3 x 10, RPE 7, 60"`` by default) unless an explicit ``text=`` is given
    (``text=""`` makes a blank cell). ``rest``/``tempo``/``note`` are the
    per-exercise columns and land on the ``ExerciseSlot`` (D2).
    """
    if text is None:
        text = compose_prescription_text(sets=sets, reps=reps, rpe=rpe, load=load)
    if exercise_slot is None:
        exercise_slot = make_slot(
            session, name=name, order=order, exercise=exercise, tags=tags
        )
    if rest or tempo or note:
        exercise_slot.rest = rest
        exercise_slot.tempo = tempo
        exercise_slot.note = note
        exercise_slot.save(update_fields=["rest", "tempo", "note"])
    if week is None:
        week = session.week
    return Prescription.objects.create(
        exercise_slot=exercise_slot, week=week, text=text, skipped=skipped
    )


def sub_line(cell, text, *, line=None):
    """A freeform sub-line cell beneath ``cell``'s row for the same week.

    ``line`` defaults to one past the row's current max line for that week.
    """
    if line is None:
        current = (
            Prescription.objects.filter(
                exercise_slot=cell.exercise_slot, week=cell.week
            )
            .order_by("-line")
            .first()
        )
        line = (current.line if current else 0) + 1
    return Prescription.objects.create(
        exercise_slot=cell.exercise_slot, week=cell.week, line=line, text=text
    )
