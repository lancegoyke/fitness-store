"""Agent slice Phase 1 — the endpoint seam + read-only review wiring.

``POST api/plan/<id>/agent/`` runs the proposal engine synchronously behind the
same ownership check as the other autosave endpoints, and returns the batch +
serialized changes. ``GET review/<batch_id>/`` renders a real batch into the
existing review screen (the bare ``review/`` stays on fixtures until approve/apply
lands in Phase 2). The Claude client is monkeypatched to a fake — these tests
never touch the network.
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


def install_fake(monkeypatch, result):
    fake = FakeClient(result)
    monkeypatch.setattr(client_module, "get_default_client", lambda: fake)
    return fake


def two_change_result(presc):
    return {
        "summary": "Knee-safe swap + a small progression.",
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


class TestAgentEndpoint:
    def test_happy_path_persists_and_returns_batch(self, client, monkeypatch):
        plan, _, presc = make_plan()
        install_fake(monkeypatch, two_change_result(presc))
        client.force_login(plan.coach)

        resp = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "Make it knee-safe."}),
            content_type="application/json",
        )

        assert resp.status_code == 201
        data = resp.json()
        assert data["ok"] is True
        assert data["summary"].startswith("Knee-safe")
        assert len(data["changes"]) == 1
        assert data["changes"][0]["title"] == "Back Squat → Box Squat"
        assert f"/meso/review/{data['batch_id']}/" in data["review_url"]
        batch = AgentProposalBatch.objects.get(pk=data["batch_id"])
        assert batch.coach == plan.coach
        assert batch.changes.count() == 1

    def test_unsafe_change_dropped_reported_as_rejected(self, client, monkeypatch):
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

        data = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        ).json()

        assert data["rejected"] == 1
        assert data["changes"] == []

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

    def test_not_configured_returns_503(self, client, monkeypatch):
        plan, _, _ = make_plan()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        client.force_login(plan.coach)
        resp = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        )
        assert resp.status_code == 503
        assert resp.json()["ok"] is False

    def test_provider_failure_returns_502(self, client, monkeypatch):
        plan, _, _ = make_plan()

        class BoomClient:
            model = "claude-opus-4-8-test"

            def propose(self, *, context, instruction):
                raise RuntimeError("provider is down")

        monkeypatch.setattr(client_module, "get_default_client", lambda: BoomClient())
        client.force_login(plan.coach)
        resp = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        )
        assert resp.status_code == 502
        assert resp.json()["ok"] is False
        assert not AgentProposalBatch.objects.exists()

    def test_non_owner_forbidden(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        client.force_login(UserFactory())
        resp = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert not AgentProposalBatch.objects.exists()

    def test_inactive_relationship_forbidden(self, client, monkeypatch):
        plan, _, _ = make_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        coach = plan.coach
        plan.relationship.end()
        client.force_login(coach)
        resp = client.post(
            agent_url(plan),
            data=json.dumps({"instruction": "go"}),
            content_type="application/json",
        )
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
