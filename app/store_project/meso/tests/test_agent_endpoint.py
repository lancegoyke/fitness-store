"""Agent slice Phase 4 — the async endpoint seam + status poll.

``POST api/plan/<id>/agent/`` now creates a ``drafting`` batch, dispatches the
proposal job (run inline under ``MESO_AGENT_RUN_SYNC`` in tests), and returns
**202** with a ``status_url``. The frontend polls
``GET api/batch/<id>/status/`` until the batch resolves to ``pending`` (changes +
review link) or ``failed`` (with the reason). Both are scoped to a batch the
requester coaches. The pre-dispatch guards (login / method / ownership / missing
key / bad instruction) still answer on the POST. The Claude client is a fake —
these never touch the network. ``GET review/<batch_id>/`` render coverage stays
in ``TestReviewBatch``.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.agent import client as client_module
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.tests.test_agent_service import FakeClient
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def agent_url(plan):
    return reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk})


def status_url(batch_id):
    return reverse("meso:api_batch_status", kwargs={"batch_id": batch_id})


def install_fake(monkeypatch, result):
    fake = FakeClient(result)
    monkeypatch.setattr(client_module, "get_default_client", lambda: fake)
    return fake


def one_swap_result(presc):
    return {
        "summary": "Knee-safe swap.",
        "changes": [
            {
                "kind": "swap",
                "prescription_id": presc.pk,
                "day_label": "Day 1 · Lower",
                "title": "Back Squat → Box Squat",
                "before": "Back Squat",
                "after": "Box Squat",
                "rationale": "Shorter range.",
                "honors": "L knee",
                "introduces_exercise": "Box Squat",
            }
        ],
    }


def propose(client, plan, instruction="Make it knee-safe."):
    return client.post(
        agent_url(plan),
        data=json.dumps({"instruction": instruction}),
        content_type="application/json",
    )


class TestAgentEndpoint:
    def test_accepts_and_dispatches_a_drafting_batch(self, client, monkeypatch):
        plan, _, presc = make_plan()
        install_fake(monkeypatch, one_swap_result(presc))
        client.force_login(plan.coach)

        resp = propose(client, plan)

        assert resp.status_code == 202
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == AgentProposalBatch.Status.DRAFTING
        assert data["status_url"] == status_url(data["batch_id"])
        # The job ran inline (MESO_AGENT_RUN_SYNC), so the row is already resolved.
        batch = AgentProposalBatch.objects.get(pk=data["batch_id"])
        assert batch.coach == plan.coach
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.changes.count() == 1

    def test_status_poll_returns_changes_when_pending(self, client, monkeypatch):
        plan, _, presc = make_plan()
        install_fake(monkeypatch, one_swap_result(presc))
        client.force_login(plan.coach)

        batch_id = propose(client, plan).json()["batch_id"]
        resp = client.get(status_url(batch_id))

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == AgentProposalBatch.Status.PENDING
        assert data["summary"].startswith("Knee-safe")
        assert len(data["changes"]) == 1
        assert data["changes"][0]["title"] == "Back Squat → Box Squat"
        assert f"/meso/review/{batch_id}/" in data["review_url"]

    def test_unsafe_change_dropped_leaves_no_changes(self, client, monkeypatch):
        from store_project.meso.factories import ContraindicationFactory

        plan, _, presc = make_plan()
        ContraindicationFactory(
            athlete=plan.athlete, text="L knee — avoid deep knee flexion under load"
        )
        install_fake(
            monkeypatch,
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
            },
        )
        client.force_login(plan.coach)

        batch_id = propose(client, plan, "go").json()["batch_id"]
        data = client.get(status_url(batch_id)).json()

        assert data["status"] == AgentProposalBatch.Status.PENDING
        assert data["changes"] == []
        assert "review_url" not in data

    def test_provider_failure_surfaces_as_failed_status(self, client, monkeypatch):
        plan, _, _ = make_plan()

        class BoomClient:
            model = "claude-opus-4-8-test"

            def propose(self, *, context, instruction):
                raise RuntimeError("provider is down")

        monkeypatch.setattr(client_module, "get_default_client", lambda: BoomClient())
        client.force_login(plan.coach)

        resp = propose(client, plan, "go")
        assert resp.status_code == 202
        batch_id = resp.json()["batch_id"]

        data = client.get(status_url(batch_id)).json()
        assert data["status"] == AgentProposalBatch.Status.FAILED
        assert "provider is down" in data["error"]

    def test_missing_instruction_400(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(plan.coach)
        resp = client.post(
            agent_url(plan), data=json.dumps({}), content_type="application/json"
        )
        assert resp.status_code == 400
        assert not AgentProposalBatch.objects.exists()

    def test_malformed_json_400(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(plan.coach)
        resp = client.post(
            agent_url(plan), data="not json", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_not_configured_returns_503_without_a_batch(self, client, monkeypatch):
        plan, _, _ = make_plan()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        client.force_login(plan.coach)
        resp = propose(client, plan, "go")
        assert resp.status_code == 503
        assert resp.json()["ok"] is False
        # The key is checked before a batch is created — nothing persisted.
        assert not AgentProposalBatch.objects.exists()

    def test_non_owner_forbidden(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(UserFactory())
        resp = propose(client, plan, "go")
        assert resp.status_code == 403
        assert not AgentProposalBatch.objects.exists()

    def test_inactive_relationship_forbidden(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        coach = plan.coach
        plan.relationship.end()
        client.force_login(coach)
        resp = propose(client, plan, "go")
        assert resp.status_code == 403

    def test_unknown_plan_404(self, client, monkeypatch):
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(UserFactory())
        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": 999999}),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_requires_login(self, client):
        plan, _, _ = make_plan()
        resp = client.post(agent_url(plan))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_get_not_allowed(self, client):
        plan, _, _ = make_plan()
        client.force_login(plan.coach)
        assert client.get(agent_url(plan)).status_code == 405


class TestBatchStatus:
    def test_drafting_batch_reports_drafting(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(
            plan=plan, coach=plan.coach, status=AgentProposalBatch.Status.DRAFTING
        )
        client.force_login(plan.coach)
        data = client.get(status_url(batch.pk)).json()
        assert data["status"] == AgentProposalBatch.Status.DRAFTING
        assert "changes" not in data

    def test_failed_batch_reports_error(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(
            plan=plan,
            coach=plan.coach,
            status=AgentProposalBatch.Status.FAILED,
            error="The agent request failed: boom",
        )
        client.force_login(plan.coach)
        data = client.get(status_url(batch.pk)).json()
        assert data["status"] == AgentProposalBatch.Status.FAILED
        assert data["error"] == "The agent request failed: boom"

    def test_non_owned_batch_404(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        client.force_login(UserFactory())
        assert client.get(status_url(batch.pk)).status_code == 404

    def test_unknown_batch_404(self, client):
        plan, _, _ = make_plan()
        client.force_login(plan.coach)
        assert client.get(status_url(999999)).status_code == 404

    def test_requires_login(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        resp = client.get(status_url(batch.pk))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_post_not_allowed(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        client.force_login(plan.coach)
        assert client.post(status_url(batch.pk)).status_code == 405


class TestReviewBatch:
    def test_renders_real_batch(self, client):
        plan, _, _ = make_plan(athlete=UserFactory(name="Maya Okonkwo"))
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        ProposedChangeFactory(batch=batch, title="Back Squat → Box Squat")
        client.force_login(plan.coach)

        resp = client.get(reverse("meso:review_batch", kwargs={"batch_id": batch.pk}))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Maya Okonkwo" in body
        assert "Back Squat → Box Squat" in body

    def test_non_owned_batch_404(self, client):
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:review_batch", kwargs={"batch_id": batch.pk}))
        assert resp.status_code == 404

    def test_bare_review_redirects_to_latest_pending_batch(self, client):
        # Fixtures are retired (Phase 2): the bare URL resolves to the coach's
        # latest pending batch on their working plan.
        plan, _, _ = make_plan()
        batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        client.force_login(plan.coach)
        resp = client.get(reverse("meso:review"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:review_batch", kwargs={"batch_id": batch.pk})

    def test_bare_review_without_a_batch_redirects_to_designer(self, client):
        plan, _, _ = make_plan()
        client.force_login(plan.coach)
        resp = client.get(reverse("meso:review"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:designer")

    def test_bare_review_finds_a_batch_on_any_owned_plan(self, client):
        # A pending batch on a non-working plan must still be reachable.
        from store_project.meso.factories import CoachAthleteFactory
        from store_project.meso.factories import PlanFactory

        coach = UserFactory()
        plan_a = PlanFactory(relationship=CoachAthleteFactory(coach=coach))
        plan_b = PlanFactory(relationship=CoachAthleteFactory(coach=coach))
        # plan_b is the more-recently-modified working plan, but the batch is on A.
        plan_b.save()
        batch = AgentProposalBatchFactory(plan=plan_a, coach=coach)
        client.force_login(coach)

        resp = client.get(reverse("meso:review"))
        assert resp.url == reverse("meso:review_batch", kwargs={"batch_id": batch.pk})
