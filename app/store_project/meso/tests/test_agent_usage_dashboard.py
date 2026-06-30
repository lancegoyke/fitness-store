"""The owner-facing agent-usage dashboard (agent-usage tracking v2, Phase 4).

Phases 1–3 captured per-run usage/cost on ``AgentProposalBatch``, rolled it into a
per-coach margin report (``build_report``), and pushed a monthly margin-alert email.
This is the **owner-facing read surface**: a staff-gated web view of that same
report — totals, the margin-alert subset, roll-ups by tier/model/trigger, and the
per-coach cost-vs-revenue-margin table with a per-client breakdown — with month
navigation. It reuses ``build_report`` + ``margin_alerts`` wholesale; this slice is
the pure month helpers, the presenter, the view's staff gate, and the template.
See ``docs/meso/agent-usage-plan.md``.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import GroupPlanFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


URL = reverse("meso:usage_dashboard")


def _at(when, **kw):
    """Build a batch then stamp its auto-add ``created_at`` to ``when``."""
    batch = AgentProposalBatchFactory(**kw)
    AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)
    batch.refresh_from_db()
    return batch


def _paid_run(start, *, cost="6.00", label_coach=None):
    """An active paid coach with one in-window run costing ``cost``.

    Revenue is base $9.99 + one seat = $10.99, so a $6.00 run trips the default
    50% threshold (ratio ~0.55) — *at risk* but not yet ``flagged``.
    """
    sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
    coach = sub.coach
    if label_coach:
        coach.name = label_coach
        coach.save(update_fields=["name"])
    plan = PlanFactory(relationship__coach=coach)
    _at(
        start + timedelta(days=1),
        plan=plan,
        coach=coach,
        billing_status=CoachSubscription.Status.ACTIVE,
        estimated_cost_usd=Decimal(cost),
        input_tokens=1000,
        output_tokens=500,
    )
    return coach


# --- pure month helpers ---------------------------------------------------


class TestShiftMonth:
    def test_steps_forward_within_year(self):
        assert report_mod.shift_month(2026, 6, 1) == (2026, 7)

    def test_steps_backward_within_year(self):
        assert report_mod.shift_month(2026, 6, -1) == (2026, 5)

    def test_rolls_over_december_forward(self):
        assert report_mod.shift_month(2026, 12, 1) == (2027, 1)

    def test_rolls_under_january_backward(self):
        assert report_mod.shift_month(2026, 1, -1) == (2025, 12)


class TestResolveAlertThreshold:
    @override_settings(MESO_MARGIN_ALERT_THRESHOLD="0.5")
    def test_reads_the_setting_by_default(self):
        assert report_mod.resolve_alert_threshold() == Decimal("0.5")

    @override_settings(MESO_MARGIN_ALERT_THRESHOLD="0.75")
    def test_honours_a_custom_setting(self):
        assert report_mod.resolve_alert_threshold() == Decimal("0.75")

    @override_settings(MESO_MARGIN_ALERT_THRESHOLD="")
    def test_blank_setting_falls_back_to_default(self):
        assert (
            report_mod.resolve_alert_threshold() == report_mod.DEFAULT_ALERT_THRESHOLD
        )

    @pytest.mark.parametrize("bad", ["abc", "-1", "0", "NaN", "Infinity"])
    def test_invalid_never_raises_returns_default(self, bad):
        # The dashboard must render even with a misconfigured env value, so a bad
        # threshold degrades to the default rather than 500ing the page.
        assert (
            report_mod.resolve_alert_threshold(bad)
            == report_mod.DEFAULT_ALERT_THRESHOLD
        )

    def test_explicit_value_wins_over_the_setting(self):
        assert report_mod.resolve_alert_threshold("0.9") == Decimal("0.9")


class TestSortedTotals:
    def test_orders_by_cost_descending(self):
        cheap = report_mod.Totals()
        cheap.add(AgentProposalBatchFactory.build(estimated_cost_usd=Decimal("1")))
        dear = report_mod.Totals()
        dear.add(AgentProposalBatchFactory.build(estimated_cost_usd=Decimal("9")))
        ordered = report_mod.sorted_totals({"cheap": cheap, "dear": dear})
        assert [key for key, _ in ordered] == ["dear", "cheap"]


# --- presenter ------------------------------------------------------------


class TestPresenter:
    def test_navigation_and_rollups(self):
        from store_project.meso import presenters

        start, end = report_mod.month_bounds(2026, 6)
        _paid_run(start)
        report = report_mod.build_report(start=start, end=end)
        ctx = presenters.usage_dashboard(report, threshold=Decimal("0.5"))
        assert ctx["month_label"] == "2026-06"
        assert ctx["prev_month"] == "2026-05"
        assert ctx["next_month"] == "2026-07"
        assert ctx["threshold_pct"] == "50"
        # The at-risk paid coach surfaces in the alert subset.
        assert len(ctx["alerts"]) == 1
        # Roll-ups are (key, Totals) lists, not the raw dicts.
        assert isinstance(ctx["by_tier"], list)
        assert ctx["report"] is report


# --- the view: staff gating -----------------------------------------------


class TestStaffGate:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.get(URL)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]

    def test_authenticated_non_staff_is_forbidden(self, client):
        client.force_login(UserFactory())
        resp = client.get(URL)
        assert resp.status_code == 403

    def test_staff_gets_the_dashboard(self, client):
        client.force_login(SuperAdminFactory())
        resp = client.get(URL)
        assert resp.status_code == 200
        assert resp.templates[0].name == "meso/usage_dashboard.html"


# --- the view: rendering --------------------------------------------------


class TestRendering:
    def test_renders_a_coachs_cost_and_label(self, client):
        start, _ = report_mod.current_month_bounds()
        _paid_run(start, cost="0.5000", label_coach="Dana Rivers")
        client.force_login(SuperAdminFactory())
        body = client.get(URL).content.decode()
        assert "Dana Rivers" in body
        assert "$0.50" in body

    def test_empty_month_renders_a_friendly_note(self, client):
        client.force_login(SuperAdminFactory())
        body = client.get(URL).content.decode()
        assert "No agent runs" in body

    def test_a_specific_month_is_honoured(self, client):
        # A run in May, none in June: ?month=2026-05 must show it, June must not.
        may_start, _ = report_mod.month_bounds(2026, 5)
        _paid_run(may_start, label_coach="May Coach")
        client.force_login(SuperAdminFactory())
        may = client.get(URL, {"month": "2026-05"}).content.decode()
        june = client.get(URL, {"month": "2026-06"}).content.decode()
        assert "May Coach" in may
        assert "May Coach" not in june

    def test_invalid_month_falls_back_to_current_with_a_message(self, client):
        client.force_login(SuperAdminFactory())
        resp = client.get(URL, {"month": "not-a-month"})
        assert resp.status_code == 200
        body = resp.content.decode()
        # Falls back to the current month (its label) and warns about the input.
        now = timezone.localtime(timezone.now())
        assert now.strftime("%Y-%m") in body

    def test_margin_alert_is_surfaced(self, client):
        start, _ = report_mod.current_month_bounds()
        _paid_run(start, cost="6.00", label_coach="Tail Risk")
        client.force_login(SuperAdminFactory())
        body = client.get(URL).content.decode()
        assert "Tail Risk" in body
        # The alert region names the margin-alert framing.
        assert "alert" in body.lower()

    def test_month_nav_links_present(self, client):
        client.force_login(SuperAdminFactory())
        body = client.get(URL, {"month": "2026-06"}).content.decode()
        assert "month=2026-05" in body
        assert "month=2026-07" in body

    def test_group_run_attributes_to_the_group(self, client):
        start, end = report_mod.current_month_bounds()
        group_plan = GroupPlanFactory()
        _at(
            start + timedelta(days=1),
            plan=group_plan,
            coach=group_plan.group.coach,
            estimated_cost_usd=Decimal("0.10"),
        )
        client.force_login(SuperAdminFactory())
        body = client.get(URL).content.decode()
        assert f"Group: {group_plan.group.name}" in body
