"""The agent usage + margin report (agent-usage tracking v2, Phase 2 — report).

Phase 1 captured per-run usage/cost on ``AgentProposalBatch`` (the per-run ledger);
this is the read side. ``billing/agent_usage_report.build_report`` rolls a calendar
month's non-eval runs up into per-coach cost-vs-revenue margins, a per-client
breakdown, and roll-ups by model / trigger / billing tier; the
``meso_agent_usage_report`` command renders it. Covers the month window, the
margin math, the COGS-vs-CAC tier split, group-vs-athlete attribution, the
eval-exclusion, and the unknown-model (unpriced) path. See
``docs/meso/agent-usage-plan.md``.
"""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription

pytestmark = pytest.mark.django_db


def _at(when, **kw):
    """Build a batch then stamp its auto-add ``created_at`` to ``when``."""
    batch = AgentProposalBatchFactory(**kw)
    AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)
    batch.refresh_from_db()
    return batch


# --- pure helpers ---------------------------------------------------------


class TestPureHelpers:
    def test_parse_month_ok(self):
        assert report_mod.parse_month("2026-06") == (2026, 6)

    @pytest.mark.parametrize("bad", ["2026", "2026/06", "June", "2026-13", ""])
    def test_parse_month_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            report_mod.parse_month(bad)

    def test_month_bounds_is_half_open_and_tz_aware(self):
        start, end = report_mod.month_bounds(2026, 6)
        assert (start.year, start.month, start.day) == (2026, 6, 1)
        assert (end.year, end.month, end.day) == (2026, 7, 1)
        assert timezone.is_aware(start) and timezone.is_aware(end)

    def test_month_bounds_rolls_over_december(self):
        _, end = report_mod.month_bounds(2026, 12)
        assert (end.year, end.month, end.day) == (2027, 1, 1)

    def test_cost_bucket_maps_each_status(self):
        S = CoachSubscription.Status
        assert report_mod.cost_bucket(S.ACTIVE) == report_mod.PAID
        assert report_mod.cost_bucket(S.PAST_DUE) == report_mod.PAID
        assert report_mod.cost_bucket(S.COMPED) == report_mod.COMPED
        assert report_mod.cost_bucket(S.FREE) == report_mod.FREE_TRIAL
        assert report_mod.cost_bucket(S.TRIALING) == report_mod.FREE_TRIAL
        assert report_mod.cost_bucket(S.CANCELED) == report_mod.FREE_TRIAL
        assert report_mod.cost_bucket("") == report_mod.FREE_TRIAL  # legacy blank

    def test_monthly_revenue_paid_is_base_plus_seats(self):
        rev = report_mod.monthly_revenue(CoachSubscription.Status.ACTIVE, 3)
        assert rev == report_mod.BASE_PRICE_USD + report_mod.SEAT_PRICE_USD * 3
        assert rev == Decimal("12.99")

    def test_monthly_revenue_past_due_still_bills(self):
        rev = report_mod.monthly_revenue(CoachSubscription.Status.PAST_DUE, 2)
        assert rev == Decimal("11.99")

    @pytest.mark.parametrize(
        "status",
        [
            CoachSubscription.Status.FREE,
            CoachSubscription.Status.TRIALING,
            CoachSubscription.Status.COMPED,
            CoachSubscription.Status.CANCELED,
        ],
    )
    def test_monthly_revenue_unpaid_is_zero(self, status):
        assert report_mod.monthly_revenue(status, 10) == Decimal("0")


# --- month windowing ------------------------------------------------------


class TestMonthWindow:
    def test_only_runs_inside_the_window_count(self):
        start, end = report_mod.month_bounds(2026, 6)
        inside = _at(start + timedelta(days=5))
        _at(start - timedelta(seconds=1))  # last instant of May
        _at(end)  # first instant of July (end is exclusive)

        report = report_mod.build_report(start=start, end=end)
        assert report.totals.runs == 1
        assert report.coaches[0].coach_id == inside.coach_id


# --- attribution ----------------------------------------------------------


class TestAttribution:
    def test_individual_run_attributes_to_the_athlete(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        _at(start + timedelta(days=1), plan=plan, coach=plan.relationship.coach)

        report = report_mod.build_report(start=start, end=end)
        (coach,) = report.coaches
        (client,) = coach.clients
        assert client.is_group is False
        assert client.label == plan.relationship.athlete.display_name()

    def test_group_run_attributes_to_the_group(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = GroupPlanFactory()
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=plan.group.coach,
            trigger=AgentProposalBatch.Trigger.GROUP,
        )

        report = report_mod.build_report(start=start, end=end)
        (coach,) = report.coaches
        (client,) = coach.clients
        assert client.is_group is True
        assert client.label == f"Group: {plan.group.name}"

    def test_two_runs_for_one_athlete_collapse_to_one_client(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            estimated_cost_usd=Decimal("0.01"),
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            estimated_cost_usd=Decimal("0.02"),
        )

        report = report_mod.build_report(start=start, end=end)
        (c,) = report.coaches
        assert len(c.clients) == 1
        assert c.clients[0].totals.runs == 2
        assert c.clients[0].totals.cost == Decimal("0.03")


# --- eval exclusion -------------------------------------------------------


class TestEvalExclusion:
    def test_eval_runs_are_excluded_but_counted(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            trigger=AgentProposalBatch.Trigger.MANUAL,
            estimated_cost_usd=Decimal("0.05"),
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            trigger=AgentProposalBatch.Trigger.EVAL,
            estimated_cost_usd=Decimal("9.99"),
        )

        report = report_mod.build_report(start=start, end=end)
        assert report.totals.runs == 1
        assert report.totals.cost == Decimal("0.05")
        assert report.eval_runs_excluded == 1


# --- token + cost aggregation + unknown model -----------------------------


class TestAggregation:
    def test_token_buckets_and_total_sum(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=300,
            estimated_cost_usd=Decimal("0.01"),
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=Decimal("0.02"),
        )

        t = report_mod.build_report(start=start, end=end).totals
        assert t.input_tokens == 110
        assert t.output_tokens == 55
        assert t.cache_creation_input_tokens == 20
        assert t.cache_read_input_tokens == 300
        assert t.total_tokens == 110 + 55 + 20 + 300
        assert t.cost == Decimal("0.03")

    def test_unknown_model_cost_is_not_counted_as_zero(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            estimated_cost_usd=None,
            model="some-unpriced-model",
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            estimated_cost_usd=Decimal("0.04"),
        )

        t = report_mod.build_report(start=start, end=end).totals
        assert t.runs == 2
        assert t.cost == Decimal("0.04")
        assert t.unknown_cost_runs == 1


# --- margin + flagging + revenue ------------------------------------------


class TestMargin:
    def test_paid_coach_with_cost_under_revenue_is_not_flagged(self):
        start, end = report_mod.month_bounds(2026, 6)
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = PlanFactory(relationship__coach=coach)
        CoachAthleteFactory(coach=coach)  # one extra billable seat → revenue 10.99+...
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.ACTIVE,
            estimated_cost_usd=Decimal("0.50"),
        )

        (c,) = report_mod.build_report(start=start, end=end).coaches
        assert c.is_paid is True
        assert c.revenue > c.totals.cost
        assert c.margin == c.revenue - Decimal("0.50")
        assert c.flagged is False

    def test_paid_coach_whose_cost_exceeds_revenue_is_flagged(self):
        start, end = report_mod.month_bounds(2026, 6)
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = PlanFactory(relationship__coach=coach)
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.ACTIVE,
            estimated_cost_usd=Decimal("99.00"),
        )

        (c,) = report_mod.build_report(start=start, end=end).coaches
        assert c.flagged is True
        assert c.margin < 0

    def test_free_coach_is_never_flagged_even_with_cost(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()  # coach has no subscription row → free
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=plan.relationship.coach,
            billing_status=CoachSubscription.Status.FREE,
            estimated_cost_usd=Decimal("5.00"),
        )

        (c,) = report_mod.build_report(start=start, end=end).coaches
        assert c.is_paid is False
        assert c.revenue == Decimal("0")
        assert c.flagged is False

    def test_revenue_reflects_current_billable_seats(self):
        start, end = report_mod.month_bounds(2026, 6)
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = PlanFactory(relationship__coach=coach)
        # The plan's own relationship is one billable seat; add two more.
        CoachAthleteFactory(coach=coach)
        CoachAthleteFactory(coach=coach)
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            estimated_cost_usd=Decimal("0.10"),
        )

        (c,) = report_mod.build_report(start=start, end=end).coaches
        assert c.billable_seats == 3
        assert c.revenue == Decimal("9.99") + Decimal("3")


# --- tier / model / trigger roll-ups --------------------------------------


class TestRollups:
    def test_tier_split_cogs_vs_cac_vs_comped(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.ACTIVE,
            estimated_cost_usd=Decimal("0.10"),
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.FREE,
            estimated_cost_usd=Decimal("0.20"),
        )
        _at(
            start + timedelta(days=3),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.COMPED,
            estimated_cost_usd=Decimal("0.30"),
        )

        tiers = report_mod.build_report(start=start, end=end).by_tier
        assert tiers[report_mod.PAID].cost == Decimal("0.10")
        assert tiers[report_mod.FREE_TRIAL].cost == Decimal("0.20")
        assert tiers[report_mod.COMPED].cost == Decimal("0.30")

    def test_model_and_trigger_rollups(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        coach = plan.relationship.coach
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            model="claude-opus-4-8",
            trigger=AgentProposalBatch.Trigger.MANUAL,
            estimated_cost_usd=Decimal("0.10"),
        )
        _at(
            start + timedelta(days=2),
            plan=plan,
            coach=coach,
            model="claude-sonnet-4-6",
            trigger=AgentProposalBatch.Trigger.DRAFT,
            estimated_cost_usd=Decimal("0.20"),
        )

        report = report_mod.build_report(start=start, end=end)
        assert report.by_model["claude-opus-4-8"].cost == Decimal("0.10")
        assert report.by_model["claude-sonnet-4-6"].cost == Decimal("0.20")
        assert report.by_trigger["Manual"].cost == Decimal("0.10")
        assert report.by_trigger["Draft with AI"].cost == Decimal("0.20")


# --- ordering -------------------------------------------------------------


def test_coaches_sorted_by_cost_descending():
    start, end = report_mod.month_bounds(2026, 6)
    cheap_plan = PlanFactory()
    dear_plan = PlanFactory()
    _at(
        start + timedelta(days=1),
        plan=cheap_plan,
        coach=cheap_plan.relationship.coach,
        estimated_cost_usd=Decimal("0.01"),
    )
    _at(
        start + timedelta(days=1),
        plan=dear_plan,
        coach=dear_plan.relationship.coach,
        estimated_cost_usd=Decimal("9.00"),
    )

    report = report_mod.build_report(start=start, end=end)
    assert report.coaches[0].coach_id == dear_plan.relationship.coach_id
    assert report.coaches[1].coach_id == cheap_plan.relationship.coach_id


# --- management command ---------------------------------------------------


class TestCommand:
    def test_text_report_renders_coach_and_flag(self):
        start, end = report_mod.month_bounds(2026, 6)
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = PlanFactory(relationship__coach=coach)
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.ACTIVE,
            estimated_cost_usd=Decimal("99.00"),
        )

        out = StringIO()
        call_command("meso_agent_usage_report", "--month", "2026-06", stdout=out)
        body = out.getvalue()
        assert "2026-06" in body
        assert coach.display_name() in body
        assert "[FLAG]" in body

    def test_empty_month_reports_no_runs(self):
        out = StringIO()
        call_command("meso_agent_usage_report", "--month", "2020-01", stdout=out)
        assert "No agent runs in this month." in out.getvalue()

    def test_json_output_is_machine_readable(self):
        start, end = report_mod.month_bounds(2026, 6)
        plan = PlanFactory()
        _at(
            start + timedelta(days=1),
            plan=plan,
            coach=plan.relationship.coach,
            estimated_cost_usd=Decimal("0.10"),
        )

        out = StringIO()
        call_command(
            "meso_agent_usage_report", "--month", "2026-06", "--json", stdout=out
        )
        data = json.loads(out.getvalue())
        assert data["totals"]["runs"] == 1
        # Cost rides as a string (Decimal-safe); the DB field keeps 6 places.
        assert Decimal(data["totals"]["cost"]) == Decimal("0.10")
        assert len(data["coaches"]) == 1

    def test_bad_month_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command("meso_agent_usage_report", "--month", "nonsense")
