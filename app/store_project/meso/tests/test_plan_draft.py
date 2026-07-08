"""Agent-drafted starter plan (Q2 fast-follow to the first-time-UX slice).

When a coach creates a *new* individual program they can ask the agent to draft
the first week: ``plan_create`` accepts a ``draft`` flag, and on a freshly-created
scaffold it reserves an agent run, creates a ``drafting`` batch with the canned
``DRAFT_INSTRUCTION``, and dispatches the proposal job (run inline under
``MESO_AGENT_RUN_SYNC`` in tests). The draft lands in the existing review gate —
no auto-apply. The agent fills the bare scaffold via the new ``add`` kind (a new
exercise row per session).

The draft is metered exactly like the manual agent endpoint (it *is* an
``AgentProposalBatch``), gated behind ``can_use_agent``, and only fires when the
plan was actually created — never overwriting an existing program. When the agent
is unavailable (allowance exhausted / no API key) the plan is still created blank
and a flash explains why.
"""

import pytest
from django.urls import reverse

from store_project.meso.agent import apply as agent_apply
from store_project.meso.agent import client as client_module
from store_project.meso.agent import service as agent_service
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.meso.models import ProposedChange
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class DraftingClient:
    """A fake agent that drafts an ``add`` row onto every session in the context.

    It reads the session ids from the grounding context the service passes it, so
    it produces real, in-plan ``add`` changes against whatever scaffold
    ``plan_create`` just built — no network, deterministic.
    """

    model = "claude-opus-4-8-test"

    def __init__(self):
        self.context = None
        self.instruction = None

    def propose(self, *, context, instruction):
        self.context = context
        self.instruction = instruction
        changes = [
            {
                "kind": "add",
                "session_id": session["id"],
                "title": f"Add accessory to {session['name']}",
                "rationale": "Drafted accessory work for the goal.",
                "new_name": "Romanian Deadlift",
                "new_sets": "3",
                "new_reps": "8-10",
                "new_rpe": "7",
            }
            for session in context["plan"]["program"]
        ]
        return {"summary": "Drafted an initial training week.", "changes": changes}


def _install(monkeypatch, client):
    monkeypatch.setattr(client_module, "get_default_client", lambda: client)
    return client


def _plan_new_url(athlete):
    return reverse("meso:plan_create", kwargs={"pk": athlete.pk})


def _draft(client, athlete):
    return client.post(_plan_new_url(athlete), data={"draft": "agent"})


class TestDraftKickoff:
    def test_draft_creates_a_pending_batch_of_add_changes(self, client, monkeypatch):
        link = CoachAthleteFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        resp = _draft(client, link.athlete)
        assert resp.status_code == 302

        plan = link.working_plan()
        batch = plan.proposal_batches.get()
        # The job ran inline (sync) so the batch is already resolved.
        assert batch.status == AgentProposalBatch.Status.PENDING
        assert batch.coach == link.coach
        assert batch.instruction == agent_service.DRAFT_INSTRUCTION
        changes = list(batch.changes.all())
        # The scaffold has two sessions, so the drafting fake adds one row to each.
        assert len(changes) == 2
        assert all(c.kind == ProposedChange.Kind.ADD for c in changes)

    def test_draft_lands_in_the_designer(self, client, monkeypatch):
        link = CoachAthleteFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        resp = _draft(client, link.athlete)
        plan = link.working_plan()
        assert resp.url == reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})

    def test_drafted_changes_add_real_rows_when_applied(self, client, monkeypatch):
        # End to end: draft → review gate → apply grows each day's grid.
        link = CoachAthleteFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        _draft(client, link.athlete)
        plan = link.working_plan()
        batch = plan.proposal_batches.get()

        from store_project.meso.serializers import current_week

        week = current_week(plan)
        before = {s.pk: s.cells().count() for s in week.sessions.all()}

        agent_apply.apply_batch(batch)

        after = {s.pk: s.cells().count() for s in week.sessions.all()}
        for pk, count in before.items():
            assert after[pk] == count + 1

    def test_draft_only_fires_on_a_freshly_created_plan(self, client, monkeypatch):
        # A relationship that already has a working plan is reopened, not
        # re-drafted — the agent must never overwrite an existing program.
        link = CoachAthleteFactory()
        link.create_plan()  # an existing working plan
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        _draft(client, link.athlete)

        assert AgentProposalBatch.objects.filter(plan__relationship=link).count() == 0

    def test_plain_create_makes_no_batch(self, client, monkeypatch):
        # Without the draft flag, plan creation is unchanged — no agent run.
        link = CoachAthleteFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        client.post(_plan_new_url(link.athlete))  # no draft flag

        assert AgentProposalBatch.objects.count() == 0


class TestDraftGating:
    def test_exhausted_allowance_creates_the_plan_blank(self, client, monkeypatch):
        # A free coach out of agent runs gets the plan (blank) but no draft batch.
        link = CoachAthleteFactory()
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(coach=link.coach)
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        resp = _draft(client, link.athlete)
        assert resp.status_code == 302

        plan = link.working_plan()
        assert plan is not None
        # Only the pre-existing allowance batches exist; no new draft batch.
        assert plan.proposal_batches.count() == 0

    def test_no_api_key_creates_the_plan_blank(self, client, monkeypatch):
        link = CoachAthleteFactory()
        monkeypatch.setattr(client_module, "get_default_client", lambda: None)
        client.force_login(link.coach)

        resp = _draft(client, link.athlete)
        assert resp.status_code == 302

        plan = link.working_plan()
        assert plan is not None
        assert plan.proposal_batches.count() == 0

    def test_comped_coach_can_draft(self, client, monkeypatch):
        # An unlimited (comped) coach is never metered — the draft runs.
        link = CoachAthleteFactory()
        CoachSubscription.comp(link.coach)
        _install(monkeypatch, DraftingClient())
        client.force_login(link.coach)

        _draft(client, link.athlete)

        plan = link.working_plan()
        assert plan.proposal_batches.get().changes.count() == 2

    def test_foreign_athlete_is_404(self, client, monkeypatch):
        link = CoachAthleteFactory()
        stranger = UserFactory()
        _install(monkeypatch, DraftingClient())
        client.force_login(stranger)

        resp = _draft(client, link.athlete)
        assert resp.status_code == 404


class TestDraftCTAs:
    """The "Draft with AI" entry points are gated on the agent allowance."""

    def test_athlete_profile_offers_draft_when_available(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete.pk})
        ).content.decode()
        assert 'name="draft" value="agent"' in body
        assert "Draft with AI" in body

    def test_athlete_profile_hides_draft_when_exhausted(self, client):
        link = CoachAthleteFactory()
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(coach=link.coach)
        client.force_login(link.coach)

        body = client.get(
            reverse("meso:athlete", kwargs={"pk": link.athlete.pk})
        ).content.decode()
        assert 'name="draft" value="agent"' not in body
        # The plain create CTA is still there.
        assert "Build a program" in body

    def test_roster_new_program_disclosure_offers_draft(self, client):
        link = CoachAthleteFactory()
        client.force_login(link.coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert 'name="draft" value="agent"' in body

    def test_roster_hides_draft_for_athlete_with_a_plan(self, client):
        # Drafting only runs for a fresh plan; for an athlete who already has one
        # the CTA would be a no-op, so the roster hides it (the plain "New
        # program" reopen stays).
        link = CoachAthleteFactory()
        link.create_plan()
        client.force_login(link.coach)

        body = client.get(reverse("meso:roster")).content.decode()
        assert 'name="draft" value="agent"' not in body
        assert "New program" in body
