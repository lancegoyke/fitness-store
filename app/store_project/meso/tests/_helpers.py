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
    sets="3",
    reps="10",
    load="60",
    load_type=None,
    rpe="7",
    rest="",
    note="",
    skipped=False,
    swap_name="",
    swap_exercise=None,
):
    """A ``Prescription`` cell (+ its ``ExerciseSlot``); mirrors the old factory.

    Pass a ``Session`` (old style): the row lands on its slot, the cell on its
    week. Or pass ``exercise_slot=`` and ``week=`` explicitly. Returns the cell.
    """
    if exercise_slot is None:
        exercise_slot = make_slot(
            session, name=name, order=order, exercise=exercise, tags=tags
        )
    if week is None:
        week = session.week
    fields = dict(
        sets=sets,
        reps=reps,
        load=load,
        rpe=rpe,
        rest=rest,
        note=note,
        skipped=skipped,
        swap_name=swap_name,
        swap_exercise=swap_exercise,
    )
    if load_type is not None:
        fields["load_type"] = load_type
    return Prescription.objects.create(exercise_slot=exercise_slot, week=week, **fields)
