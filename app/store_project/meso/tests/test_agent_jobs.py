"""Agent slice Phase 4 — the background job + drafting/failed lifecycle.

The proposal run is split so it can happen off the request thread: the endpoint
creates a ``drafting`` batch and dispatches ``run_proposal_job``, which grounds →
calls Claude → validates → persists, flipping the batch to ``pending`` (ready for
review) or ``failed`` (with the reason recorded). ``dispatch_proposal`` runs the
job inline under the test setting ``MESO_AGENT_RUN_SYNC`` so these never spawn a
thread or touch the network — the client is a fake.
"""

import pytest

from store_project.meso.agent import jobs
from store_project.meso.agent import service
from store_project.meso.models import AgentProposalBatch
from store_project.meso.tests.test_agent_service import FakeClient
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def one_swap(presc):
    return {
        "summary": "Knee-safe swap.",
        "changes": [
            {
                "kind": "swap",
                "prescription_id": presc.pk,
                "title": "Back Squat → Box Squat",
                "before": "Back Squat",
                "after": "Box Squat",
                "rationale": "Shorter range.",
                "introduces_exercise": "Box Squat",
            }
        ],
    }


class TestDraftingBatch:
    def test_create_drafting_batch_is_drafting_with_no_changes(self):
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(
            plan, "Make it knee-safe.", coach=plan.coach
        )
        assert batch.status == AgentProposalBatch.Status.DRAFTING
        assert batch.instruction == "Make it knee-safe."
        assert batch.coach == plan.coach
        assert batch.changes.count() == 0


class TestRunProposalJob:
    def test_happy_path_flips_drafting_to_pending(self):
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        fake = FakeClient(one_swap(presc))

        result_batch, rejected = service.run_proposal_job(batch.pk, client=fake)

        result_batch.refresh_from_db()
        assert result_batch.pk == batch.pk
        assert result_batch.status == AgentProposalBatch.Status.PENDING
        assert result_batch.summary == "Knee-safe swap."
        assert result_batch.model == "claude-opus-4-8-test"
        assert result_batch.error == ""
        assert rejected == []
        assert result_batch.changes.count() == 1

    def test_provider_failure_marks_batch_failed(self):
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)

        class BoomClient:
            model = "claude-opus-4-8-test"

            def propose(self, *, context, instruction):
                raise RuntimeError("provider is down")

        service.run_proposal_job(batch.pk, client=BoomClient())

        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.FAILED
        assert "provider is down" in batch.error
        assert batch.changes.count() == 0

    def test_not_configured_marks_batch_failed(self, monkeypatch):
        from store_project.meso.agent import client as client_module

        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)

        service.run_proposal_job(batch.pk)

        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.FAILED
        assert batch.error

    def test_unsafe_change_is_dropped_but_batch_still_pending(self):
        from store_project.meso.factories import ContraindicationFactory

        plan, _, presc = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete,
            text="L knee — avoid deep knee flexion under load",
        )
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        fake = FakeClient(
            {
                "summary": "",
                "changes": [
                    {
                        "kind": "swap",
                        "prescription_id": presc.pk,
                        "title": "Back Squat → Deep Knee Flexion Drill",
                        "rationale": "...",
                        "introduces_exercise": "Deep Knee Flexion Drill",
                    }
                ],
            }
        )

        result_batch, rejected = service.run_proposal_job(batch.pk, client=fake)

        result_batch.refresh_from_db()
        assert result_batch.status == AgentProposalBatch.Status.PENDING
        assert result_batch.changes.count() == 0
        assert len(rejected) == 1


class TestDispatch:
    def test_dispatch_runs_inline_under_sync_setting(self, settings):
        # The test settings set MESO_AGENT_RUN_SYNC; dispatch must run the job
        # inline so the batch is resolved by the time dispatch returns.
        settings.MESO_AGENT_RUN_SYNC = True
        plan, _, presc = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)
        fake = FakeClient(one_swap(presc))

        jobs.dispatch_proposal(batch.pk, client=fake)

        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.changes.count() == 1

    def test_threaded_dispatch_defers_the_thread_to_on_commit(
        self, settings, monkeypatch, django_capture_on_commit_callbacks
    ):
        # ATOMIC_REQUESTS: the thread must not start until the request commits,
        # or it would query the drafting batch before it is visible. Stub the
        # thread launch so no real thread runs here.
        settings.MESO_AGENT_RUN_SYNC = False
        started = []
        monkeypatch.setattr(
            jobs, "_start_thread", lambda batch_id, client: started.append(batch_id)
        )
        plan, _, _ = make_plan()
        batch = service.create_drafting_batch(plan, "go", coach=plan.coach)

        with django_capture_on_commit_callbacks(execute=True):
            jobs.dispatch_proposal(batch.pk)
            # Deferred — nothing launched until the surrounding block commits.
            assert started == []

        assert started == [batch.pk]
