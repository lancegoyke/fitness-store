"""The agent margin-threshold alert (agent-usage tracking v2, Phase 3).

Phase 2 (``build_report``) computes each paying coach's cost vs revenue and a
binary ``flagged`` (cost already exceeds revenue — margin gone negative). This is
the **early-warning** layer: a tunable threshold (default 50% of revenue) plus a
proactive owner email and a monthly ``qcluster`` sweep, so a coach whose agent
cost is *eating into* — not yet exceeding — their plan surfaces before the month
closes. Covers the pure ``at_risk`` / ``cost_to_revenue_ratio`` / ``margin_alerts``
logic, the previous-month window, the owner email, and the management command.
See ``docs/meso/agent-usage-plan.md``.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.notifications import emails

pytestmark = pytest.mark.django_db


def _at(when, **kw):
    """Build a batch then stamp its auto-add ``created_at`` to ``when``."""
    batch = AgentProposalBatchFactory(**kw)
    AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)
    batch.refresh_from_db()
    return batch


def _coach_usage(*, cost, revenue, is_paid=True, label="Coach", runs=1):
    """A bare :class:`CoachUsage` for the pure-logic and email tests (no DB)."""
    totals = report_mod.Totals()
    totals.runs = runs
    totals.cost = Decimal(cost)
    return report_mod.CoachUsage(
        coach_id="x",
        label=label,
        billing_status=CoachSubscription.Status.ACTIVE if is_paid else "free",
        is_paid=is_paid,
        billable_seats=1,
        revenue=Decimal(revenue),
        totals=totals,
        clients=[],
    )


def _paid_at_risk_run(start, *, cost="12.00"):
    """An active paid coach with one in-window run whose cost is >50% of revenue.

    Revenue is the flat Pro price $19 (D14), so a $12.00 run trips the default 50%
    threshold (ratio ~0.63) without yet exceeding revenue (so it is *at risk* but
    not ``flagged``).
    """
    sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
    coach = sub.coach
    plan = PlanFactory(relationship__coach=coach)
    _at(
        start + timedelta(days=1),
        plan=plan,
        coach=coach,
        billing_status=CoachSubscription.Status.ACTIVE,
        estimated_cost_usd=Decimal(cost),
    )
    return coach


# --- pure logic: ratio / at_risk / margin_alerts --------------------------


class TestRatioAndAtRisk:
    def test_ratio_is_cost_over_revenue(self):
        c = _coach_usage(cost="5.00", revenue="10.00")
        assert c.cost_to_revenue_ratio == Decimal("0.5")

    def test_ratio_is_none_when_no_revenue(self):
        c = _coach_usage(cost="5.00", revenue="0", is_paid=False)
        assert c.cost_to_revenue_ratio is None

    def test_at_risk_when_cost_exceeds_threshold_fraction(self):
        c = _coach_usage(cost="6.00", revenue="10.00")
        assert c.at_risk(Decimal("0.5")) is True

    def test_not_at_risk_below_threshold(self):
        c = _coach_usage(cost="4.00", revenue="10.00")
        assert c.at_risk(Decimal("0.5")) is False

    def test_threshold_boundary_is_strict(self):
        # Exactly at the threshold is not "over" it.
        c = _coach_usage(cost="5.00", revenue="10.00")
        assert c.at_risk(Decimal("0.5")) is False

    def test_free_coach_is_never_at_risk(self):
        # $0 revenue by design — cost is CAC, not a margin problem (mirrors flagged).
        c = _coach_usage(cost="100.00", revenue="0", is_paid=False)
        assert c.at_risk(Decimal("0.5")) is False

    def test_at_risk_generalizes_flagged_at_one(self):
        # flagged == cost > revenue == at_risk(1).
        c = _coach_usage(cost="11.00", revenue="10.00")
        assert c.flagged is True
        assert c.at_risk(Decimal("1")) is True


class TestMarginAlerts:
    def test_collects_only_at_risk_coaches_sorted_by_ratio(self):
        worst = _coach_usage(cost="9.00", revenue="10.00", label="Worst")  # 0.90
        mild = _coach_usage(cost="6.00", revenue="10.00", label="Mild")  # 0.60
        safe = _coach_usage(cost="1.00", revenue="10.00", label="Safe")  # 0.10
        report = report_mod.Report(
            start=None,
            end=None,
            coaches=[mild, safe, worst],
            by_model={},
            by_trigger={},
            by_tier={},
            totals=report_mod.Totals(),
            eval_runs_excluded=0,
        )
        alerts = report_mod.margin_alerts(report, Decimal("0.5"))
        assert [c.label for c in alerts] == ["Worst", "Mild"]

    def test_higher_threshold_shrinks_the_set(self):
        coach = _coach_usage(cost="7.00", revenue="10.00")  # 0.70
        report = report_mod.Report(
            start=None,
            end=None,
            coaches=[coach],
            by_model={},
            by_trigger={},
            by_tier={},
            totals=report_mod.Totals(),
            eval_runs_excluded=0,
        )
        assert report_mod.margin_alerts(report, Decimal("0.5")) == [coach]
        assert report_mod.margin_alerts(report, Decimal("0.9")) == []


# --- previous-month window ------------------------------------------------


class TestPreviousMonthBounds:
    def test_previous_month_is_the_month_before_current(self):
        from django.utils import timezone

        now = timezone.localtime(timezone.now())
        year, month = (
            (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        )
        assert report_mod.previous_month_bounds() == report_mod.month_bounds(
            year, month
        )

    def test_previous_month_is_disjoint_and_earlier_than_current(self):
        prev_start, prev_end = report_mod.previous_month_bounds()
        cur_start, _ = report_mod.current_month_bounds()
        assert prev_end == cur_start
        assert prev_start < prev_end


# --- the owner email ------------------------------------------------------


class TestMarginAlertEmail:
    def test_emails_admins_with_coach_and_threshold(self):
        alerts = [_coach_usage(cost="6.00", revenue="10.99", label="Risky Rita")]
        sent = emails.send_margin_alert_email(
            alerts=alerts, month_label="2026-06", threshold=Decimal("0.5")
        )
        assert sent is True
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["lance@lancegoyke.com"]  # settings.ADMINS
        body = msg.body
        assert "Risky Rita" in body
        assert "2026-06" in body
        assert "50%" in body

    def test_no_email_when_no_alerts(self):
        sent = emails.send_margin_alert_email(
            alerts=[], month_label="2026-06", threshold=Decimal("0.5")
        )
        assert sent is False
        assert mail.outbox == []


# --- management command ---------------------------------------------------


class TestCommand:
    def test_sends_alert_for_at_risk_paid_coach(self):
        start, _ = report_mod.month_bounds(2026, 6)
        coach = _paid_at_risk_run(start)

        out = StringIO()
        call_command("meso_agent_margin_alert", "--month", "2026-06", stdout=out)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["lance@lancegoyke.com"]
        body = out.getvalue()
        assert coach.display_name() in body
        assert "1" in body  # one coach at risk

    def test_dry_run_reports_but_sends_nothing(self):
        start, _ = report_mod.month_bounds(2026, 6)
        coach = _paid_at_risk_run(start)

        out = StringIO()
        call_command(
            "meso_agent_margin_alert", "--month", "2026-06", "--dry-run", stdout=out
        )

        assert mail.outbox == []
        assert coach.display_name() in out.getvalue()

    def test_no_alert_when_all_within_threshold(self):
        start, _ = report_mod.month_bounds(2026, 6)
        _paid_at_risk_run(start, cost="0.10")  # ratio ~0.009 — safe

        out = StringIO()
        call_command("meso_agent_margin_alert", "--month", "2026-06", stdout=out)

        assert mail.outbox == []
        assert "0" in out.getvalue()

    def test_threshold_override_changes_the_set(self):
        start, _ = report_mod.month_bounds(2026, 6)
        _paid_at_risk_run(start, cost="12.00")  # ratio ~0.63 of $19

        out = StringIO()
        call_command(
            "meso_agent_margin_alert",
            "--month",
            "2026-06",
            "--threshold",
            "0.9",
            stdout=out,
        )
        assert mail.outbox == []  # 0.70 < 0.90

    def test_setting_supplies_the_default_threshold(self):
        start, _ = report_mod.month_bounds(2026, 6)
        _paid_at_risk_run(start, cost="12.00")  # ratio ~0.63

        with override_settings(MESO_MARGIN_ALERT_THRESHOLD="0.9"):
            call_command("meso_agent_margin_alert", "--month", "2026-06")
        assert mail.outbox == []  # default raised above the run's ratio

    def test_blank_setting_falls_back_to_default_not_error(self):
        # A blank MESO_MARGIN_ALERT_THRESHOLD= env value must not crash the
        # scheduled run — it falls back to the documented 0.5 default.
        start, _ = report_mod.month_bounds(2026, 6)
        coach = _paid_at_risk_run(start, cost="12.00")  # ratio ~0.63 > 0.5

        with override_settings(MESO_MARGIN_ALERT_THRESHOLD=""):
            call_command("meso_agent_margin_alert", "--month", "2026-06")
        assert len(mail.outbox) == 1
        assert coach.display_name() in mail.outbox[0].body

    def test_last_month_window(self):
        prev_start, _ = report_mod.previous_month_bounds()
        coach = _paid_at_risk_run(prev_start)

        call_command("meso_agent_margin_alert", "--last-month")
        assert len(mail.outbox) == 1
        assert coach.display_name() in mail.outbox[0].body

    def test_bad_month_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command("meso_agent_margin_alert", "--month", "nonsense")

    def test_bad_threshold_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command("meso_agent_margin_alert", "--threshold", "abc")

    def test_nonpositive_threshold_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command("meso_agent_margin_alert", "--threshold", "0")

    @pytest.mark.parametrize("bad", ["Infinity", "NaN", "-1"])
    def test_non_finite_or_negative_threshold_raises_command_error(self, bad):
        # Decimal() accepts Infinity/NaN; a non-finite threshold would silently
        # suppress every alert (cost > Infinity) or crash the <= comparison (NaN).
        with pytest.raises(CommandError):
            call_command("meso_agent_margin_alert", "--threshold", bad)
