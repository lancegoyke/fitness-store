"""Agent slice Phase 2 — the review-gate endpoints (approve/reject + apply).

The HTTP seam over ``agent.apply``: per-change approve/reject persistence, the
batch apply action (writes approved edits back into the program + marks the batch
applied), and dismiss. Every endpoint is scoped to a batch the requester coaches
over an active relationship — a foreign or unknown batch is a 404, never a silent
write.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def status_url(change):
    return reverse("meso:api_change_status", kwargs={"pk": change.pk})


def apply_url(batch):
    return reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})


def dismiss_url(batch):
    return reverse("meso:api_batch_dismiss", kwargs={"batch_id": batch.pk})


def make_batch_with_swap():
    plan, _, presc = make_plan()
    batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
    change = ProposedChangeFactory(
        batch=batch,
        kind=ProposedChange.Kind.SWAP,
        prescription=presc,
        payload={"name": "Box Squat"},
    )
    return plan, presc, batch, change


class TestChangeStatus:
    def test_approve_persists(self, client):
        plan, _, _, change = make_batch_with_swap()
        client.force_login(plan.coach)
        resp = client.post(
            status_url(change),
            data=json.dumps({"status": "rejected"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        change.refresh_from_db()
        assert change.status == ProposedChange.Status.REJECTED

    def test_invalid_status_400(self, client):
        plan, _, _, change = make_batch_with_swap()
        client.force_login(plan.coach)
        resp = client.post(
            status_url(change),
            data=json.dumps({"status": "maybe"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_non_owner_404(self, client):
        _, _, _, change = make_batch_with_swap()
        client.force_login(UserFactory())
        resp = client.post(
            status_url(change),
            data=json.dumps({"status": "approved"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_status_on_resolved_batch_409(self, client):
        plan, _, batch, change = make_batch_with_swap()
        batch.status = AgentProposalBatch.Status.APPLIED
        batch.save(update_fields=["status"])
        client.force_login(plan.coach)
        resp = client.post(
            status_url(change),
            data=json.dumps({"status": "rejected"}),
            content_type="application/json",
        )
        assert resp.status_code == 409

    def test_requires_login(self, client):
        _, _, _, change = make_batch_with_swap()
        resp = client.post(status_url(change))
        assert resp.status_code == 302

    def test_get_not_allowed(self, client):
        plan, _, _, change = make_batch_with_swap()
        client.force_login(plan.coach)
        assert client.get(status_url(change)).status_code == 405


class TestBatchApply:
    def test_applies_approved_changes_and_marks_batch(self, client):
        plan, presc, batch, _ = make_batch_with_swap()
        client.force_login(plan.coach)
        resp = client.post(apply_url(batch))

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["applied"] == 1
        assert f"/meso/deliver/{plan.pk}/" in data["deliver_url"]
        presc.refresh_from_db()
        batch.refresh_from_db()
        assert presc.name == "Box Squat"
        assert batch.status == AgentProposalBatch.Status.APPLIED

    def test_rejected_change_is_not_applied(self, client):
        plan, presc, batch, change = make_batch_with_swap()
        change.status = ProposedChange.Status.REJECTED
        change.save(update_fields=["status"])
        client.force_login(plan.coach)
        client.post(apply_url(batch))
        presc.refresh_from_db()
        assert presc.name == "Back Squat"

    def test_already_applied_409(self, client):
        plan, _, batch, _ = make_batch_with_swap()
        batch.status = AgentProposalBatch.Status.APPLIED
        batch.save(update_fields=["status"])
        client.force_login(plan.coach)
        assert client.post(apply_url(batch)).status_code == 409

    def test_non_owner_404(self, client):
        _, _, batch, _ = make_batch_with_swap()
        client.force_login(UserFactory())
        assert client.post(apply_url(batch)).status_code == 404

    def test_requires_login(self, client):
        _, _, batch, _ = make_batch_with_swap()
        assert client.post(apply_url(batch)).status_code == 302

    def test_get_not_allowed(self, client):
        plan, _, batch, _ = make_batch_with_swap()
        client.force_login(plan.coach)
        assert client.get(apply_url(batch)).status_code == 405


class TestBatchApplyDeliverUrl:
    """§4b FIX 3: the post-apply deliver URL is pinned to the batch's own block.

    A bare ``/meso/deliver/<plan>/`` resolves its week via ``current_week(plan)``
    — the plan's earliest LIVE week, i.e. block 1 — so a coach who ran the agent
    on block 2 would land on block 1 and see none of the changes they just
    applied. ``batch_apply`` appends ``?week=<the batch's block's first live
    week>`` so the deliver screen opens on the block that was actually edited.
    """

    def test_deliver_url_carries_the_batchs_block_first_live_week(self, client):
        plan, presc, batch, _ = make_batch_with_swap()  # block 1 from make_plan()
        meso2 = MesocycleFactory(plan=plan, order=1)
        week2 = WeekFactory(mesocycle=meso2, index=1)
        WeekFactory(mesocycle=meso2, index=2)  # a later week — "first" must win
        batch.mesocycle = meso2
        batch.save(update_fields=["mesocycle"])
        client.force_login(plan.coach)

        resp = client.post(apply_url(batch))

        assert resp.status_code == 200
        expected = (
            reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
            + f"?week={week2.pk}"
        )
        assert resp.json()["deliver_url"] == expected

    def test_batch_with_no_block_returns_the_bare_deliver_url(self, client):
        # ``mesocycle`` is nullable (``SET_NULL``) for legacy rows and any
        # caller that never pinned a block (the eval harness, direct/test
        # callers) — must degrade to the bare URL, not error.
        plan, _, batch, _ = make_batch_with_swap()
        assert batch.mesocycle_id is None  # factory default: no block pinned
        client.force_login(plan.coach)

        resp = client.post(apply_url(batch))

        assert resp.status_code == 200
        assert resp.json()["deliver_url"] == reverse(
            "meso:deliver_plan", kwargs={"plan_id": plan.pk}
        )


class TestBatchDismiss:
    def test_dismiss_marks_batch(self, client):
        plan, presc, batch, _ = make_batch_with_swap()
        client.force_login(plan.coach)
        resp = client.post(dismiss_url(batch))

        assert resp.status_code == 200
        batch.refresh_from_db()
        presc.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.DISMISSED
        assert presc.name == "Back Squat"  # nothing applied

    def test_already_resolved_409(self, client):
        plan, _, batch, _ = make_batch_with_swap()
        batch.status = AgentProposalBatch.Status.DISMISSED
        batch.save(update_fields=["status"])
        client.force_login(plan.coach)
        assert client.post(dismiss_url(batch)).status_code == 409

    def test_non_owner_404(self, client):
        _, _, batch, _ = make_batch_with_swap()
        client.force_login(UserFactory())
        assert client.post(dismiss_url(batch)).status_code == 404
