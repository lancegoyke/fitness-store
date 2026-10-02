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


def legacy_move_exercise_to_session(cell, target_session, *, index=0):
    """Build LEGACY data: a slot moved by the retired ``prescription_move`` endpoint.

    The endpoint is gone (a move is now delete + re-add) but old prod data
    still has slots re-pointed this way, and the read paths must keep coping
    with it. This reproduces what the endpoint did, straight in the ORM:
    re-point ``cell.exercise_slot.session_slot`` (block-wide) to the target
    day's ``SessionSlot``, densely renumber (0-based) both the source and the
    target day's live slots with the moved one landing at ``index``, and give
    every other week of the block a ``Session`` for the target ``SessionSlot``
    (so the moved slot has a day to render on in each week). The athlete's
    ``LoggedSet`` rows are left alone, as the endpoint left them.
    """
    es = cell.exercise_slot
    source_slot = es.session_slot
    target_slot = target_session.session_slot
    live = ExerciseSlot.objects.filter(deleted_at__isnull=True)
    target_rows = list(live.filter(session_slot=target_slot).order_by("order"))
    target_rows.insert(max(0, min(index, len(target_rows))), es)
    for new_order, row in enumerate(target_rows):
        ExerciseSlot.objects.filter(pk=row.pk).update(
            order=new_order, session_slot_id=target_slot.pk
        )
    if source_slot.pk != target_slot.pk:
        source_rows = (
            live.filter(session_slot=source_slot).exclude(pk=es.pk).order_by("order")
        )
        for new_order, row in enumerate(source_rows):
            ExerciseSlot.objects.filter(pk=row.pk).update(order=new_order)
    for week in target_session.week.mesocycle.weeks.filter(deleted_at__isnull=True):
        if not Session.objects.filter(week=week, session_slot=target_slot).exists():
            Session.objects.create(week=week, session_slot=target_slot)
    es.refresh_from_db()
    cell.exercise_slot = es


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


def sub_line(cell, text, *, line=None, athlete_authored=False):
    """A freeform sub-line cell beneath ``cell``'s row for the same week.

    ``line`` defaults to one past the row's current max line for that week.

    ``athlete_authored`` defaults False (a coach-written sub-line), matching
    most callers. Pass True to simulate one the athlete typed — the only kind
    ``athlete_cell_write`` produces, and the flag the 5a no-double-display
    suppression keys on, so a parse-at-commit fixture MUST set it or it models
    a state the app can't reach.
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
        exercise_slot=cell.exercise_slot,
        week=cell.week,
        line=line,
        text=text,
        athlete_authored=athlete_authored,
    )
