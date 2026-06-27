"""Agent slice Phase 2 — applying an approved batch back into the program.

``agent.apply`` is the write side of the review gate: once a coach approves
changes, ``apply_batch`` performs the structured edit each ``ProposedChange``
describes (swap → prescription name; progress → load; volume → sets; deload →
flag the week), marks the batch applied, and leaves rejected changes untouched.
Every write is deterministic and unit-tested here; the endpoint is the thin HTTP
seam over this (see ``test_agent_apply_endpoint``).
"""

import pytest

from store_project.meso.agent import apply as agent_apply
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange
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
