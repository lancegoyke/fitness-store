"""S6 — billing, the flat monthly Pro plan (D14).

The pivot from base+per-seat (D13) to a single **flat monthly Pro price**: per-seat
billing was dropped (the athlete isn't the cost driver — the AI agent is), so the
agent is now metered at *every* tier except comped, and the paywall surfaces speak
one flat price. These tests pin the flat-model behaviours the refactor introduced:

- ``presenters.agent_allowance`` meters a paid coach too (tier ``paid``, the larger
  ``PAID_AGENT_ALLOWANCE``) and only exempts ``comped`` (tier ``unlimited``);
- the agent endpoint 402s a *paid* coach at their cap with ``upgrade=False`` (no
  higher tier to sell — the copy points at the monthly reset), distinct from a free
  coach's upgrade 402;
- the shared ``PRICE_SUMMARY`` copy is the one flat price.

See ``docs/meso/billing-plan.md``.
"""

import json

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# presenters.agent_allowance — the tiered meter
# ---------------------------------------------------------------------------


class TestAgentAllowancePresenter:
    def test_free_coach_is_metered_at_the_free_allowance(self):
        coach = UserFactory()  # no row → free
        meter = presenters.agent_allowance(coach)
        assert meter["metered"] is True
        assert meter["tier"] == "free"
        assert meter["allowance"] == CoachSubscription.FREE_AGENT_ALLOWANCE
        assert meter["remaining"] == CoachSubscription.FREE_AGENT_ALLOWANCE
        assert meter["can_use"] is True

    @pytest.mark.parametrize(
        "status",
        [CoachSubscription.Status.TRIALING, CoachSubscription.Status.ACTIVE],
    )
    def test_paid_coach_is_metered_at_the_paid_allowance(self, status):
        sub = CoachSubscriptionFactory(status=status)
        meter = presenters.agent_allowance(sub.coach)
        assert meter["metered"] is True
        assert meter["tier"] == "paid"
        assert meter["allowance"] == CoachSubscription.PAID_AGENT_ALLOWANCE

    def test_comped_coach_is_unlimited(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.COMPED)
        meter = presenters.agent_allowance(sub.coach)
        assert meter["metered"] is False
        assert meter["tier"] == "unlimited"
        assert meter["remaining"] is None
        assert meter["can_use"] is True

    def test_used_reflects_runs_this_month(self):
        coach = UserFactory()
        AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        meter = presenters.agent_allowance(coach)
        assert meter["used"] == 2
        assert meter["remaining"] == CoachSubscription.FREE_AGENT_ALLOWANCE - 2


# ---------------------------------------------------------------------------
# the agent endpoint — tier-aware 402 at the cap
# ---------------------------------------------------------------------------


def _plan_for(coach):
    rel = CoachAthleteFactory(
        coach=coach, athlete=UserFactory(), status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    presc(session, name="Back Squat")
    return plan


class TestPaidCapGate:
    def test_paid_coach_at_cap_gets_402_without_upgrade(self, client):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = _plan_for(coach)
        # Exhaust the paid allowance (one query, one plan).
        AgentProposalBatch.objects.bulk_create(
            [
                AgentProposalBatch(coach=coach, plan=plan, instruction="x")
                for _ in range(CoachSubscription.PAID_AGENT_ALLOWANCE)
            ]
        )
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk}),
            data=json.dumps({"instruction": "Make it knee-safe."}),
            content_type="application/json",
        )
        assert resp.status_code == 402
        body = resp.json()
        assert body["ok"] is False
        # A paid coach is already on the top plan — no upgrade, just a reset note.
        assert body["upgrade"] is False
        assert str(CoachSubscription.PAID_AGENT_ALLOWANCE) in body["error"]


# ---------------------------------------------------------------------------
# the shared paywall copy
# ---------------------------------------------------------------------------


def test_price_summary_is_the_flat_price():
    assert presenters.PRICE_SUMMARY == "$19/mo — unlimited athletes"


# ---------------------------------------------------------------------------
# the roster billing card — comped coaches aren't shown a broken meter
# ---------------------------------------------------------------------------


class TestRosterBillingCard:
    def test_comped_coach_shows_unlimited_not_a_broken_meter(self, client):
        # A comped coach is active but unmetered (allowance 0, remaining None), so
        # the active-tier line must not render "None of 0 agent runs left".
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        CoachSubscription.comp(coach)
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "unlimited agent runs" in body
        assert "None of 0" not in body

    def test_paid_coach_shows_the_agent_meter(self, client):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.ACTIVE)
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert (
            f"of {CoachSubscription.PAID_AGENT_ALLOWANCE} agent runs left this month"
            in body
        )
