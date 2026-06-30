"""The coach-facing billing & usage page (agent-usage tracking — coach surface).

The owner dashboard (``UsageDashboardView``, Phase 4) is **staff-gated** and shows
org-wide *cost* (COGS) so a coach can't probe what the agent costs the business.
This is the complementary **coach-scoped** read: a coach sees *their* plan, the
seats they pay for, their projected bill (the revenue they owe — base + per-seat),
and how many AI-agent runs they've spent this month broken down by athlete/group.

The hard line this slice draws: a coach sees **what they pay** (revenue) and
**how much they've used** (run counts), never the internal per-run **cost**
estimate (``estimated_cost_usd``) — that's owner-only. The tests below pin both
the projected-bill math and that no internal cost figure leaks onto the page.
See ``docs/meso/agent-usage-plan.md``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.meso.presenters import coach_billing
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = reverse("meso:billing")


def _coach():
    """A user who counts as a coach (``_is_coach`` true via a ``CoachProfile``)."""
    return CoachProfileFactory().user


def _run_for(coach, *, plan=None, when=None, **kw):
    """One in-window agent run for ``coach`` (``created_at`` stamped to ``when``)."""
    if plan is None:
        plan = PlanFactory(relationship__coach=coach)
    batch = AgentProposalBatchFactory(plan=plan, coach=coach, **kw)
    if when is not None:
        AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)
        batch.refresh_from_db()
    return batch


# -- coach_run_breakdown (the per-client run-count helper) -----------------


class TestCoachRunBreakdown:
    def test_groups_a_coachs_runs_by_client_sorted_by_count(self):
        coach = _coach()
        heavy = PlanFactory(relationship__coach=coach)
        light = PlanFactory(relationship__coach=coach)
        for _ in range(3):
            _run_for(coach, plan=heavy)
        _run_for(coach, plan=light)

        start, end = report_mod.current_month_bounds()
        rows = report_mod.coach_run_breakdown(coach, start=start, end=end)

        assert [r.runs for r in rows] == [3, 1]  # heaviest client first
        labels = {r.label: r.runs for r in rows}
        assert labels[heavy.relationship.athlete.display_name()] == 3
        assert labels[light.relationship.athlete.display_name()] == 1

    def test_attributes_a_group_plan_to_the_group(self):
        coach = _coach()
        group_plan = GroupPlanFactory(group__coach=coach, group__name="Squad A")
        _run_for(coach, plan=group_plan, trigger=AgentProposalBatch.Trigger.GROUP)

        start, end = report_mod.current_month_bounds()
        (row,) = report_mod.coach_run_breakdown(coach, start=start, end=end)

        assert row.is_group is True
        assert row.label == "Group: Squad A"
        assert row.runs == 1

    def test_scopes_to_the_coach(self):
        coach = _coach()
        other = _coach()
        _run_for(coach)
        _run_for(other)

        start, end = report_mod.current_month_bounds()
        rows = report_mod.coach_run_breakdown(coach, start=start, end=end)

        assert sum(r.runs for r in rows) == 1

    def test_windows_to_the_month(self):
        coach = _coach()
        start, end = report_mod.current_month_bounds()
        _run_for(coach, when=start + timedelta(days=1))  # in window
        _run_for(coach, when=start - timedelta(days=1))  # previous month

        rows = report_mod.coach_run_breakdown(coach, start=start, end=end)

        assert sum(r.runs for r in rows) == 1


# -- coach_billing (the presenter) -----------------------------------------


class TestCoachBillingPresenter:
    def test_projected_bill_is_base_plus_per_seat(self):
        coach = _coach()
        for _ in range(3):
            CoachAthleteFactory(coach=coach)  # 3 billable seats

        ctx = coach_billing(coach)

        assert ctx["seats"] == 3
        assert ctx["billed_seats"] == 3
        assert ctx["base_price"] == Decimal("9.99")
        assert ctx["seat_cost"] == Decimal("3.00")
        assert ctx["projected_total"] == Decimal("12.99")

    def test_projected_bill_floors_billed_seats_at_one(self):
        # A coach with zero active athletes still pays for one seat (Stripe floors
        # the seat quantity at 1), so the projection is base + one seat, not bare base.
        coach = _coach()

        ctx = coach_billing(coach)

        assert ctx["seats"] == 0
        assert ctx["billed_seats"] == 1
        assert ctx["projected_total"] == Decimal("10.99")

    def test_runs_this_month_matches_the_breakdown(self):
        coach = _coach()
        plan = PlanFactory(relationship__coach=coach)
        for _ in range(4):
            _run_for(coach, plan=plan)

        ctx = coach_billing(coach)

        assert ctx["runs_this_month"] == 4
        assert sum(r.runs for r in ctx["breakdown"]) == 4

    def test_carries_billing_state_and_agent_allowance(self):
        coach = _coach()  # free, no subscription row

        ctx = coach_billing(coach)

        assert ctx["state"]["status"] == CoachSubscription.Status.FREE
        # The free-tier agent meter rides along for the "N of M runs left" line.
        assert (
            ctx["state"]["agent"]["allowance"] == CoachSubscription.FREE_AGENT_ALLOWANCE
        )

    def test_does_not_leak_internal_cost(self):
        coach = _coach()
        _run_for(coach, estimated_cost_usd=Decimal("8.123456"))

        ctx = coach_billing(coach)

        # The coach surface is revenue + run counts only — no COGS estimate.
        assert "cost" not in ctx
        assert "margin" not in ctx
        for row in ctx["breakdown"]:
            assert not hasattr(row, "cost")
            assert not hasattr(row, "estimated_cost_usd")


# -- BillingView (the page + its gate) -------------------------------------


class TestBillingView:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.get(URL)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]

    def test_non_coach_is_redirected_to_training_home(self, client):
        client.force_login(UserFactory())  # a pure athlete — no coach signal
        resp = client.get(URL)
        assert resp.status_code == 302
        assert resp["Location"] == reverse("meso:athlete_home")

    def test_coach_sees_plan_bill_and_breakdown(self, client):
        coach = _coach()
        plan = PlanFactory(relationship__coach=coach)
        athlete_name = plan.relationship.athlete.display_name()
        _run_for(coach, plan=plan)

        client.force_login(coach)
        resp = client.get(URL)
        body = resp.content.decode()

        assert resp.status_code == 200
        assert "10.99" in body  # base $9.99 + the one seat = projected bill
        assert athlete_name in body  # the per-athlete run breakdown

    def test_only_shows_the_coachs_own_runs(self, client):
        coach = _coach()
        other = _coach()
        other_plan = PlanFactory(relationship__coach=other)
        other_name = other_plan.relationship.athlete.display_name()
        _run_for(other, plan=other_plan)

        client.force_login(coach)
        resp = client.get(URL)

        assert other_name not in resp.content.decode()

    def test_internal_cost_estimate_never_renders(self, client):
        coach = CoachSubscriptionFactory(
            status=CoachSubscription.Status.ACTIVE,
            coach=_coach(),
        ).coach
        CoachAthleteFactory(coach=coach)
        _run_for(coach, estimated_cost_usd=Decimal("8.123456"))

        client.force_login(coach)
        body = client.get(URL).content.decode()

        assert "8.12" not in body  # the COGS estimate is owner-only
