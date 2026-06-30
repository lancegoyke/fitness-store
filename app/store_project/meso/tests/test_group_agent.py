"""Group agent — Phase 1: the AI agent edits a group's SHARED program.

The agent used to reject a group plan with a 400 — its grounding dereferenced a
single ``plan.athlete``. This slice grounds it on the *group* instead (the active
members and their contraindications **folded across all of them**) and lets it
propose edits to the shared program behind the same propose → review → apply gate
every individual run uses. Two properties matter:

- **Safety is conservative.** The contraindication backstop folds *every* active
  member's active contraindications, so a swap/add unsafe for **any one** member
  is rejected — the shared row trains everyone.
- **The edit lands on the shared template.** ``agent.apply`` writes onto the
  shared ``ExercisePrescription``, so every member inherits it (per-member
  auto-adjust generation stays a later phase).

The review/status/apply endpoints are scoped to a plan the coach may *edit*
(``editable_by``), which now includes their group plans — a foreign coach still
404s. A group run is tagged ``trigger=group`` for the usage ledger (attributed to
the group, athlete null).
"""

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.agent import apply as agent_apply
from store_project.meso.agent import service
from store_project.meso.agent import validation
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import GroupMembershipFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import ProposedChange
from store_project.meso.serializers import current_week
from store_project.meso.tests.test_agent_endpoint import install_fake
from store_project.meso.tests.test_agent_endpoint import propose
from store_project.meso.tests.test_agent_endpoint import status_url
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_group_plan(*, coach=None, members=2):
    """A comped coach's group with ``members`` active athletes + a shared plan.

    Comped so the paid-only agent gate (S6) stays open. Returns
    ``(coach, group, plan, athletes)`` where ``plan`` is the group's scaffolded
    shared program (a current week with two starter days).
    """
    coach = coach or UserFactory()
    CoachSubscription.comp(coach)
    group = MesoGroupFactory(coach=coach)
    athletes = [
        GroupMembershipFactory(group=group).relationship.athlete for _ in range(members)
    ]
    plan = group.create_shared_plan()
    return coach, group, plan, athletes


def first_presc(plan):
    """A shared prescription in the group plan's current week."""
    week = current_week(plan)
    return week.sessions.first().prescriptions.first()


def swap_result(presc, *, introduces, title="New exercise → swap"):
    return {
        "summary": "Swapped a shared lift.",
        "changes": [
            {
                "kind": "swap",
                "prescription_id": presc.pk,
                "day_label": "Day 1",
                "title": title,
                "before": "New exercise",
                "after": introduces,
                "rationale": "Fits the group's focus.",
                "honors": "",
                "introduces_exercise": introduces,
            }
        ],
    }


class TestGroupContext:
    def test_group_plan_grounds_on_the_group_not_an_athlete(self):
        coach, group, plan, athletes = make_group_plan()
        ctx = service.build_context(plan)
        # A group has no single athlete to ground on.
        assert "athlete" not in ctx
        assert ctx["recent_logs"] == []
        g = ctx["group"]
        assert g["name"] == group.name
        assert g["member_count"] == 2

    def test_group_context_folds_member_contraindications(self):
        coach, group, plan, athletes = make_group_plan()
        ContraindicationFactory(
            athlete=athletes[0], text="L knee — avoid deep knee flexion under load"
        )
        ContraindicationFactory(athlete=athletes[1], text="No overhead pressing")

        g = service.build_context(plan)["group"]

        folded = " ".join(g["contraindications"]).lower()
        assert "knee flexion" in folded
        assert "overhead pressing" in folded
        # Each member also carries their own, so the agent knows whose is whose.
        per_member = {len(m["contraindications"]) for m in g["members"]}
        assert per_member == {1}

    def test_individual_plan_context_is_unchanged(self):
        # The individual path keeps its athlete block + recent_logs contract.
        from store_project.meso.tests.test_agent_validation import make_plan

        plan, _, _ = make_plan()
        ctx = service.build_context(plan)
        assert "group" not in ctx
        assert "name" in ctx["athlete"]
        assert ctx["recent_logs"] == []


class TestGroupForbiddenTerms:
    def test_folds_across_active_members(self):
        coach, group, plan, athletes = make_group_plan()
        ContraindicationFactory(
            athlete=athletes[0], text="L knee — avoid deep knee flexion under load"
        )
        ContraindicationFactory(
            athlete=athletes[1], text="No overhead pressing movements"
        )

        terms = validation.forbidden_terms(plan)

        assert "flexion" in terms  # member 0
        assert "overhead" in terms  # member 1
        assert "pressing" in terms

    def test_excludes_an_ended_members_contraindication(self):
        coach, group, plan, athletes = make_group_plan(members=1)
        gone = GroupMembershipFactory(group=group)
        ContraindicationFactory(
            athlete=gone.relationship.athlete, text="No deadlifting"
        )
        gone.relationship.status = CoachAthlete.Status.ENDED
        gone.relationship.save(update_fields=["status"])

        assert "deadlifting" not in validation.forbidden_terms(plan)


class TestGroupAgentEndpoint:
    def test_accepts_a_group_plan_and_tags_the_run(self, client, monkeypatch):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        install_fake(monkeypatch, swap_result(presc, introduces="Front Squat"))
        client.force_login(coach)

        resp = propose(client, plan, "progress the group")

        assert resp.status_code == 202
        batch = AgentProposalBatch.objects.get(pk=resp.json()["batch_id"])
        assert batch.plan_id == plan.pk
        assert batch.coach == coach
        # A group run attributes to the group (athlete null) and is tagged group.
        assert batch.trigger == AgentProposalBatch.Trigger.GROUP
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.changes.count() == 1

    def test_status_poll_returns_the_group_batch(self, client, monkeypatch):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        install_fake(monkeypatch, swap_result(presc, introduces="Front Squat"))
        client.force_login(coach)

        batch_id = propose(client, plan).json()["batch_id"]
        data = client.get(status_url(batch_id)).json()

        assert data["status"] == AgentProposalBatch.Status.PENDING
        assert len(data["changes"]) == 1
        assert f"/meso/review/{batch_id}/" in data["review_url"]

    def test_folded_backstop_drops_a_swap_unsafe_for_any_member(
        self, client, monkeypatch
    ):
        coach, group, plan, athletes = make_group_plan()
        # Only the SECOND member flags knee flexion; the shared row trains both,
        # so the swap must still be rejected.
        ContraindicationFactory(
            athlete=athletes[1], text="L knee — avoid deep knee flexion under load"
        )
        presc = first_presc(plan)
        install_fake(
            monkeypatch,
            swap_result(presc, introduces="Deep Knee Flexion Drill"),
        )
        client.force_login(coach)

        batch_id = propose(client, plan).json()["batch_id"]
        data = client.get(status_url(batch_id)).json()

        assert data["status"] == AgentProposalBatch.Status.PENDING
        assert data["changes"] == []  # dropped by the folded backstop
        assert "review_url" not in data

    def test_foreign_coach_cannot_propose_on_a_group(self, client, monkeypatch):
        coach, group, plan, athletes = make_group_plan()
        install_fake(monkeypatch, {"summary": "", "changes": []})
        other = UserFactory()
        CoachSubscription.comp(other)
        client.force_login(other)

        resp = propose(client, plan, "go")

        # The plan exists but the foreigner can't edit it → 403 (mirrors the
        # individual ``test_non_owner_forbidden``), and nothing is persisted.
        assert resp.status_code == 403
        assert not AgentProposalBatch.objects.exists()


class TestGroupReviewApply:
    def make_group_batch(self, *, status=AgentProposalBatch.Status.PENDING):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        batch = AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction="swap it",
            status=status,
            trigger=AgentProposalBatch.Trigger.GROUP,
        )
        change = ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Front Squat"},
        )
        return coach, plan, presc, batch, change

    def test_status_endpoint_serves_a_group_batch(self, client):
        coach, plan, presc, batch, change = self.make_group_batch()
        client.force_login(coach)
        resp = client.get(status_url(batch.pk))
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentProposalBatch.Status.PENDING

    def test_review_screen_renders_a_group_batch(self, client):
        coach, plan, presc, batch, change = self.make_group_batch()
        client.force_login(coach)
        resp = client.get(reverse("meso:review_batch", kwargs={"batch_id": batch.pk}))
        assert resp.status_code == 200
        # The review heading names the group, not a (None) athlete.
        assert plan.group.name in resp.content.decode()

    def test_apply_writes_onto_the_shared_prescription(self, client):
        coach, plan, presc, batch, change = self.make_group_batch()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 200
        presc.refresh_from_db()
        # Every member trains off this shared row, so the swap reaches all of them.
        assert presc.name == "Front Squat"
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED

    def test_foreign_coach_cannot_review_a_group_batch(self, client):
        coach, plan, presc, batch, change = self.make_group_batch()
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:review_batch", kwargs={"batch_id": batch.pk}))
        assert resp.status_code == 404

    def test_apply_sends_a_group_batch_back_to_the_designer(self, client):
        # A group plan has no individual deliver screen (delivery is to-all), so the
        # post-apply redirect must land on the designer, not a 404ing deliver page.
        coach, plan, presc, batch, change = self.make_group_batch()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 200
        assert resp.json()["deliver_url"] == reverse(
            "meso:designer_plan", kwargs={"plan_id": plan.pk}
        )
        # And that deliver screen really would 404 for a group plan (why we reroute).
        assert (
            client.get(
                reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
            ).status_code
            == 404
        )


class TestReviewPresenter:
    def test_group_batch_subject_is_the_group_name(self):
        coach, group, plan, athletes = make_group_plan()
        batch = AgentProposalBatch.objects.create(
            plan=plan, coach=coach, instruction="x"
        )
        ctx = presenters.review_changes(batch)
        assert ctx["athlete"]["name"] == group.name


class TestClientFraming:
    def test_group_context_adds_shared_program_framing(self):
        from store_project.meso.agent import client as agent_client

        context = {"group": {"name": "Squad", "members": []}}
        prompt = agent_client._user_prompt(context, "progress everyone")
        assert "SHARED program" in prompt
        assert "every member" in prompt.lower()

    def test_individual_context_keeps_the_plain_prompt(self):
        from store_project.meso.agent import client as agent_client

        context = {"athlete": {"name": "Maya"}}
        prompt = agent_client._user_prompt(context, "progress the squat")
        assert "SHARED program" not in prompt


class TestGroupDesignerUI:
    def _designer_template(self):
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
        )
        return path.read_text()

    def _meso_js(self):
        from pathlib import Path

        from django.contrib.staticfiles import finders

        return Path(finders.find("js/meso.js")).read_text()

    def test_stale_next_phase_placeholder_is_gone(self):
        # The "group agent arrives in the next phase" copy contradicts this slice.
        html = self._designer_template()
        js = self._meso_js()
        assert "arrives in the next phase" not in html
        assert "arrives in the next phase" not in js

    def test_meso_js_group_greeting_invites_the_agent(self):
        js = self._meso_js()
        # The group greeting invites the coach to use the agent on the shared
        # program — for the whole group or to adjust one athlete (Phase 2).
        assert "change it for the whole group" in js
        assert "adjust one athlete" in js

    def test_group_designer_renders_the_agent_composer(self, client):
        coach, group, plan, athletes = make_group_plan()
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Ask the agent to adjust the program" in body

    def test_group_designer_hydrates_a_persisted_group_thread(self, client):
        # Group plans can now have agent batches; the persisted-thread hydration
        # must not dereference a (None) athlete.
        coach, group, plan, athletes = make_group_plan()
        AgentProposalBatch.objects.create(
            plan=plan,
            coach=coach,
            instruction="lighten the squats",
            summary="Done.",
            status=AgentProposalBatch.Status.PENDING,
            trigger=AgentProposalBatch.Trigger.GROUP,
        )
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        assert "lighten the squats" in resp.content.decode()


class TestGroupApplyUnit:
    def test_apply_batch_edits_the_shared_row(self):
        coach, group, plan, athletes = make_group_plan()
        presc = first_presc(plan)
        batch = AgentProposalBatch.objects.create(
            plan=plan, coach=coach, instruction="x"
        )
        ProposedChangeFactory(
            batch=batch,
            kind=ProposedChange.Kind.SWAP,
            prescription=presc,
            payload={"name": "Romanian Deadlift"},
        )
        result = agent_apply.apply_batch(batch)
        assert result["applied"] == 1
        presc.refresh_from_db()
        assert presc.name == "Romanian Deadlift"
