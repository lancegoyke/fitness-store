"""Agent slice Phase 1 — the proposal data model.

``AgentProposalBatch`` is one agent run behind the review gate; ``ProposedChange``
is a single proposed edit it wrote (swap / progress / volume / deload), targeting
a real session/prescription within the plan. Both start ``pending`` — the coach
still approves (apply lands in Phase 2). See ``docs/archive/meso/agent-plan.md``.
"""

import pytest

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PrescriptionFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange

pytestmark = pytest.mark.django_db


class TestAgentProposalBatch:
    def test_batch_defaults_to_pending(self):
        batch = AgentProposalBatchFactory()
        assert batch.status == AgentProposalBatch.Status.PENDING

    def test_batch_belongs_to_a_plan_and_coach(self):
        batch = AgentProposalBatchFactory()
        assert batch in batch.plan.proposal_batches.all()
        assert batch in batch.coach.meso_proposal_batches.all()

    def test_changes_related_name(self):
        batch = AgentProposalBatchFactory()
        change = ProposedChangeFactory(batch=batch)
        assert change in batch.changes.all()


class TestAgentProposalBatchMesocycle:
    """§4b: the batch freezes the coach's *viewed* block at request time.

    docs/meso/remove-current-week-plan.md — grounding (a background job) and
    apply (a later request) can't re-read a live "current" pointer across
    that time gap, so it's captured once and frozen here instead.
    """

    def test_batch_can_carry_its_block(self):
        batch = AgentProposalBatchFactory()
        block = MesocycleFactory(plan=batch.plan)
        batch.mesocycle = block
        batch.save(update_fields=["mesocycle"])

        batch.refresh_from_db()
        assert batch.mesocycle == block
        assert batch in block.proposal_batches.all()

    def test_mesocycle_defaults_to_null(self):
        # ``null=True`` covers legacy rows (created before this field existed)
        # and any caller that doesn't pin a block.
        batch = AgentProposalBatchFactory()
        assert batch.mesocycle is None

    def test_hard_deleting_the_block_nulls_the_fk_and_preserves_the_batch(self):
        # ``SET_NULL`` IS LOAD-BEARING: the batch is also the usage/cost
        # ledger (agent-usage tracking v1), so deleting a block must never
        # cascade into deleting billing history.
        batch = AgentProposalBatchFactory()
        block = MesocycleFactory(plan=batch.plan)
        batch.mesocycle = block
        batch.save(update_fields=["mesocycle"])

        block.delete()

        batch.refresh_from_db()
        assert batch.mesocycle_id is None
        assert AgentProposalBatch.objects.filter(pk=batch.pk).exists()


class TestProposedChange:
    def test_change_defaults_to_pending(self):
        change = ProposedChangeFactory()
        assert change.status == ProposedChange.Status.PENDING

    def test_change_carries_review_display_fields(self):
        change = ProposedChangeFactory(
            kind=ProposedChange.Kind.SWAP,
            day_label="Day 1 · Lower",
            title="Bulgarian Split Squat → Box Step-Down (low)",
            before="Bulgarian Split Squat (DB) · 3×10 @ 18 kg",
            after="Box Step-Down (low) · 3×10 @ 14 kg",
            honors="L knee — avoid deep knee flexion under load",
        )
        assert change.kind == "swap"
        assert change.day_label == "Day 1 · Lower"
        assert "Box Step-Down" in change.title

    def test_change_can_target_a_prescription(self):
        presc = PrescriptionFactory()
        change = ProposedChangeFactory(prescription=presc)
        assert change.prescription == presc
        assert change in presc.proposed_changes.all()

    def test_changes_order_within_a_batch(self):
        batch = AgentProposalBatchFactory()
        ProposedChangeFactory(batch=batch, order=2, title="second")
        ProposedChangeFactory(batch=batch, order=1, title="first")
        titles = [c.title for c in batch.changes.all()]
        assert titles == ["first", "second"]
