"""Agent slice Phase 2 — applying an approved batch back into the program.

``agent.apply`` is the write side of the review gate: once a coach approves
changes, ``apply_batch`` performs the structured edit each ``ProposedChange``
describes (swap → prescription name; progress → load; volume → sets; deload →
flag the week), marks the batch applied, and leaves rejected changes untouched.
Every write is deterministic and unit-tested here; the endpoint is the thin HTTP
seam over this (see ``test_agent_apply_endpoint``).
"""

import pytest

from store_project.exercises.factories import ExerciseFactory
from store_project.meso.agent import apply as agent_apply
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import Prescription
from store_project.meso.models import ProposedChange
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def _batch(plan):
    return AgentProposalBatchFactory(plan=plan, coach=plan.coach)


class TestApplyChange:
    def test_swap_sets_prescription_name_from_payload(self):
        plan, _, presc = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Box Squat"},
        )
        result = agent_apply.apply_change(change)
        presc.refresh_from_db()
        assert presc.name == "Box Squat"
        assert result["field"] == "name"

    def test_swap_falls_back_to_introduces_exercise(self):
        plan, _, presc = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            introduces_exercise="Goblet Squat",
            payload={},
        )
        agent_apply.apply_change(change)
        presc.refresh_from_db()
        assert presc.name == "Goblet Squat"

    def test_swap_is_block_wide(self):
        # P4: an agent swap renames the block-shared ``ExerciseSlot``, so EVERY
        # week's cell follows (``Prescription.name`` resolves to the slot). It is
        # a slot rename, NOT a one-week cell ``swap_name`` write.
        plan, session, cell_w1 = make_plan()  # week 1 (current)
        week2 = WeekFactory(mesocycle=session.week.mesocycle, index=2)
        day(week2, session_slot=session.session_slot)
        cell_w2 = presc(exercise_slot=cell_w1.exercise_slot, week=week2)
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.SWAP,
            prescription=cell_w1,
            payload={"name": "Front Squat"},
        )
        agent_apply.apply_change(change)

        # Re-fetch fresh so the resolving ``name`` property reads the renamed slot.
        cell_w1 = Prescription.objects.get(pk=cell_w1.pk)
        cell_w2 = Prescription.objects.get(pk=cell_w2.pk)
        assert cell_w1.name == "Front Squat"
        assert cell_w2.name == "Front Squat"  # block-wide
        assert cell_w1.swap_name == ""  # slot rename, NOT a cell swap
        assert cell_w1.exercise_slot.name == "Front Squat"

    def test_swap_severs_catalog_link(self):
        # A free-text rename severs the slot's catalog FK so the row isn't
        # mis-keyed to the old exercise.
        plan, _, cell = make_plan()
        cell.exercise_slot.exercise = ExerciseFactory()
        cell.exercise_slot.save(update_fields=["exercise"])
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.SWAP,
            prescription=cell,
            payload={"name": "Front Squat"},
        )
        agent_apply.apply_change(change)
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.exercise is None

    def test_progress_sets_load(self):
        plan, _, presc = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.PROGRESS,
            prescription=presc,
            payload={"load": "92.5 kg"},
        )
        agent_apply.apply_change(change)
        presc.refresh_from_db()
        assert presc.load == "92.5 kg"

    def test_volume_sets_set_count(self):
        plan, _, presc = make_plan()  # factory default sets == "3"
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.VOLUME,
            prescription=presc,
            payload={"sets": "4"},
        )
        agent_apply.apply_change(change)
        presc.refresh_from_db()
        assert presc.sets == "4"

    def test_volume_targeting_a_session_sets_all_rows(self):
        # A volume change may target a whole day (session) rather than one row;
        # apply it across every prescription so it is never a silent no-op.
        plan, session, first_presc = make_plan()  # one row at sets == "3"
        presc2 = presc(session, name="RDL", sets="3")
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.VOLUME,
            prescription=None,
            session=session,
            payload={"sets": "4"},
        )
        result = agent_apply.apply_change(change)
        first_presc.refresh_from_db()
        presc2.refresh_from_db()
        assert first_presc.sets == "4"
        assert presc2.sets == "4"
        assert result["count"] == 2

    def test_deload_flags_the_current_week(self):
        plan, session, _ = make_plan()
        assert session.week.is_deload is False
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.DELOAD,
            prescription=None,
            session=None,
        )
        agent_apply.apply_change(change)
        session.week.refresh_from_db()
        assert session.week.is_deload is True

    def test_swap_without_name_or_prescription_is_a_noop(self):
        plan, _, _ = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.SWAP,
            prescription=None,
            introduces_exercise="",
            payload={},
        )
        assert agent_apply.apply_change(change) is None

    def test_progress_without_load_is_a_noop(self):
        plan, _, presc = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.PROGRESS,
            prescription=presc,
            payload={},
        )
        before = presc.load
        assert agent_apply.apply_change(change) is None
        presc.refresh_from_db()
        assert presc.load == before

    def test_add_creates_a_new_prescription_on_the_session(self):
        # ``add`` is the verb that lets the agent draft a program: it creates a
        # brand-new exercise row on the target session (it has no prescription to
        # edit), ordered after the existing rows.
        plan, session, presc = make_plan()  # one starter row at order 0
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.ADD,
            prescription=None,
            session=session,
            payload={
                "name": "Romanian Deadlift",
                "sets": "3",
                "reps": "8-10",
                "rpe": "7",
            },
        )
        result = agent_apply.apply_change(change)

        added = session.cells().last()
        assert added.name == "Romanian Deadlift"
        assert added.sets == "3"
        assert added.reps == "8-10"
        assert added.rpe == "7"
        assert added.exercise_slot.order > presc.exercise_slot.order
        assert result["kind"] == ProposedChange.Kind.ADD
        assert session.cells().count() == 2

    def test_add_without_a_name_is_a_noop(self):
        plan, session, _ = make_plan()
        before = session.cells().count()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.ADD,
            prescription=None,
            session=session,
            payload={"sets": "3"},
        )
        assert agent_apply.apply_change(change) is None
        assert session.cells().count() == before

    def test_add_without_a_session_is_a_noop(self):
        plan, _, _ = make_plan()
        change = ProposedChangeFactory(
            batch=_batch(plan),
            kind=ProposedChange.Kind.ADD,
            prescription=None,
            session=None,
            payload={"name": "Plank"},
        )
        assert agent_apply.apply_change(change) is None


class TestApplyBatch:
    def test_applies_non_rejected_and_marks_batch_applied(self):
        plan, _, presc = make_plan()
        batch = _batch(plan)
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Box Squat"},
            status=ProposedChange.Status.PENDING,
        )
        result = agent_apply.apply_batch(batch)

        presc.refresh_from_db()
        batch.refresh_from_db()
        assert presc.name == "Box Squat"
        assert result["applied"] == 1
        assert batch.status == AgentProposalBatch.Status.APPLIED

    def test_rejected_change_is_not_applied(self):
        plan, _, presc = make_plan()
        batch = _batch(plan)
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Box Squat"},
            status=ProposedChange.Status.REJECTED,
        )
        result = agent_apply.apply_batch(batch)

        presc.refresh_from_db()
        assert presc.name == "Back Squat"  # untouched
        assert result["applied"] == 0

    def test_bumps_plan_modified(self):
        plan, _, presc = make_plan()
        batch = _batch(plan)
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Box Squat"},
        )
        before = plan.modified
        agent_apply.apply_batch(batch)
        plan.refresh_from_db()
        assert plan.modified >= before


class TestDismissBatch:
    def test_marks_batch_dismissed(self):
        plan, _, _ = make_plan()
        batch = _batch(plan)
        agent_apply.dismiss_batch(batch)
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.DISMISSED
