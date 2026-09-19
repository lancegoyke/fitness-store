"""The coach-facing billing & usage page (agent-usage tracking — coach surface).

The owner dashboard (``UsageDashboardView``, Phase 4) is **staff-gated** and shows
org-wide *cost* (COGS) so a coach can't probe what the agent costs the business.
This is the complementary **coach-scoped** read: a coach sees *their* plan, the
seats they pay for, their projected bill (the revenue they owe — base + per-seat),
and how many AI-agent runs they've spent this month broken down by athlete.

The hard line this slice draws: a coach sees **what they pay** (revenue) and
**how much they've used** (run counts), never the internal per-run **cost**
estimate (``estimated_cost_usd``) — that's owner-only. The tests below pin both
the projected-bill math and that no internal cost figure leaks onto the page.
See ``docs/meso/agent-usage-plan.md``.
"""

import math
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import dateformat
from django.utils import timezone

from store_project.meso.billing import access as billing_access
from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.meso.presenters import coach_billing
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = reverse("meso:billing")
ROSTER_URL = reverse("meso:roster")


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
    def test_bill_is_the_flat_pro_price(self):
        # The flat plan (D14) bills one price regardless of the athlete count.
        coach = _coach()
        for _ in range(3):
            CoachAthleteFactory(coach=coach)  # 3 seats — irrelevant to the bill

        ctx = coach_billing(coach)

        assert ctx["seats"] == 3
        assert ctx["plan_price"] == Decimal("19.00")

    def test_bill_is_flat_even_with_zero_athletes(self):
        coach = _coach()

        ctx = coach_billing(coach)

        assert ctx["seats"] == 0
        assert ctx["plan_price"] == Decimal("19.00")

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
        assert "19.00" in body  # the flat Pro price
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


# -- Stripe-trial billing surfaces (#555) -----------------------------------
#
# A coach who subscribed mid-trial has a `trialing` row WITH a Stripe
# subscription id — a live Stripe subscription, not a locally-clocked one
# (`is_stripe_trial`). The billing page and the roster card both read the
# first-charge date off it and hide the Subscribe button (a second Checkout
# would double-charge).


class TestStripeTrialBillingSurfaces:
    def test_billing_page_shows_first_charge_and_hides_subscribe(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=9)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=trial_end,
        )
        client.force_login(coach)
        resp = client.get(URL)
        body = resp.content.decode()

        assert resp.status_code == 200
        expected_date = dateformat.format(trial_end, "M j")
        # The date is wrapped in a `<time>` (#555 P1-C — local-timezone rewrite),
        # so it's no longer adjacent text to "first charge on".
        assert "first charge on" in body
        assert f">{expected_date}</time>" in body
        assert 'action="/meso/billing/subscribe/"' not in body
        assert "Manage billing" in body

    def test_roster_card_shows_first_charge_and_hides_subscribe(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=9)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=trial_end,
        )
        client.force_login(coach)
        resp = client.get(ROSTER_URL)
        body = resp.content.decode()

        assert resp.status_code == 200
        expected_date = dateformat.format(trial_end, "M j")
        assert "first charge on" in body
        assert f">{expected_date}</time>" in body
        assert 'action="/meso/billing/subscribe/"' not in body
        assert "Manage billing" in body


# -- Billing dates render in the viewer's local timezone (P1-C, #555) -------
#
# The server renders UTC; ``meso_local_dates.js`` rewrites each wrapped date
# to the browser's local timezone on load (progressive enhancement — the UTC
# text is the no-JS fallback). These pin the server side: the `<time
# datetime="…" data-local-date>` wrapper and the script tag are present.


class TestBillingDatesAreTimezoneAware:
    def test_billing_page_wraps_the_deferred_date_and_loads_the_script(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        client.force_login(coach)
        body = client.get(URL).content.decode()

        expected_iso = dateformat.format(trial_end, "c")
        assert f'<time datetime="{expected_iso}" data-local-date>' in body
        assert "js/meso_local_dates.js" in body

    def test_roster_card_wraps_the_deferred_date_and_loads_the_script(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        client.force_login(coach)
        body = client.get(ROSTER_URL).content.decode()

        expected_iso = dateformat.format(trial_end, "c")
        assert f'<time datetime="{expected_iso}" data-local-date>' in body
        assert "js/meso_local_dates.js" in body

    def test_stripe_trial_first_charge_date_is_wrapped(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=9)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=trial_end,
        )
        client.force_login(coach)
        billing_body = client.get(URL).content.decode()
        roster_body = client.get(ROSTER_URL).content.decode()

        expected_iso = dateformat.format(trial_end, "c")
        assert f'<time datetime="{expected_iso}" data-local-date>' in billing_body
        assert f'<time datetime="{expected_iso}" data-local-date>' in roster_body


# -- A local trial with no clock (P2-3, #555) --------------------------------
#
# `trial_end=None` is an admin-only state (a null clock never expires) — the
# no-deferral branch must not render an empty "ends , in under 2 days."


class TestTrialWithNoClock:
    def test_billing_page_shows_no_clock_copy(self, client):
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=None,
        )
        client.force_login(coach)
        body = client.get(URL).content.decode()

        assert "Free trial. Subscribing starts billing today." in body
        assert "ends ," not in body
        assert "ends," not in body

    def test_roster_card_shows_no_clock_copy(self, client):
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=None,
        )
        client.force_login(coach)
        body = client.get(ROSTER_URL).content.decode()

        assert (
            "Free trial. Subscribing ($19/mo — unlimited athletes) starts billing"
            " today." in body
        )
        assert "ends ," not in body
        assert "ends," not in body


# -- past_due has its own state (#556, item 1) -------------------------------
#
# A past_due coach doesn't count as active, so before this both surfaces
# showed the free-plan line and a Subscribe button that opened no Checkout
# (`billing_subscribe` already refuses a live-subscription coach). past_due
# now gets its own failed-payment line, no Subscribe form, and a Manage
# billing form (primary-styled, since updating the card is the one thing to
# do).


class TestPastDueBillingSurfaces:
    def test_past_due_coach_sees_failed_payment_line_no_subscribe_manage_billing(
        self, client
    ):
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
        )
        client.force_login(coach)
        for url in (URL, ROSTER_URL):
            body = client.get(url).content.decode()
            assert (
                "Your last payment failed. Update your card in Manage billing." in body
            )
            assert 'action="/meso/billing/subscribe/"' not in body
            assert 'action="/meso/billing/portal/"' in body
            assert "meso-btn--primary" in body  # Manage billing is primary here

    def test_past_due_over_the_seat_limit_appends_the_suspended_note(self, client):
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
        )
        # Over the free seat limit (1 — CoachSubscription.FREE_SEAT_LIMIT).
        CoachAthleteFactory(coach=coach)
        CoachAthleteFactory(coach=coach)
        client.force_login(coach)
        for url in (URL, ROSTER_URL):
            body = client.get(url).content.decode()
            assert (
                "Your last payment failed. Update your card in Manage billing." in body
            )
            assert "suspended." in body
            assert 'action="/meso/billing/subscribe/"' not in body

    def test_active_coach_gets_ghost_manage_billing_not_primary(self, client):
        """Regression guard: only past_due gets the primary-styled button."""
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        client.force_login(coach)
        body = client.get(URL).content.decode()
        assert "meso-btn--primary" not in body


# -- mirroring a scheduled cancel (#556, item 3) -----------------------------
#
# `CoachSubscription.cancel_at` mirrors when a live subscription is scheduled
# to end (a Portal cancel, or the API's own `cancel_at_period_end`). The
# billing surfaces read it as "Pro until {date}" instead of promising a
# first-charge date that won't happen.


class TestCancellingBillingSurfaces:
    def test_active_row_reads_pro_until_with_the_date_wrapped_and_no_subscribe(
        self, client
    ):
        coach = _coach()
        period_end = timezone.now() + timedelta(days=12)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
            current_period_end=period_end,
            cancel_at=period_end,
        )
        client.force_login(coach)
        expected_date = dateformat.format(period_end, "M j")
        expected_iso = dateformat.format(period_end, "c")
        for url in (URL, ROSTER_URL):
            body = client.get(url).content.decode()
            assert "Pro until" in body
            assert "cancelled, so it won't renew." in body
            assert (
                f'<time datetime="{expected_iso}" data-local-date>{expected_date}'
                "</time>" in body
            )
            assert 'action="/meso/billing/subscribe/"' not in body
            assert "first charge on" not in body

    def test_stripe_trial_row_reads_pro_until_and_hides_first_charge_copy(self, client):
        coach = _coach()
        trial_end = timezone.now() + timedelta(days=9)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=trial_end,
            cancel_at=trial_end,
        )
        client.force_login(coach)
        for url in (URL, ROSTER_URL):
            body = client.get(url).content.decode()
            assert "Pro until" in body
            assert "cancelled, so you won't be charged." in body
            assert "first charge on" not in body
            assert 'action="/meso/billing/subscribe/"' not in body

    def test_access_and_seat_gate_are_unchanged_while_cancelling(self):
        coach = _coach()
        period_end = timezone.now() + timedelta(days=12)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
            current_period_end=period_end,
            cancel_at=period_end,
        )
        assert billing_access.is_active(coach) is True
        assert billing_access.can_add_athlete(coach) is True
        assert billing_access.effective_seat_limit(coach) == math.inf

    def test_past_due_wins_over_cancelling(self, client):
        """past_due has no Pro access to promise, so it takes precedence."""
        coach = _coach()
        period_end = timezone.now() + timedelta(days=12)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
            current_period_end=period_end,
            cancel_at=period_end,
        )
        client.force_login(coach)
        body = client.get(URL).content.decode()
        assert "Pro until" not in body
        assert "Your last payment failed" in body


# -- the `?billing=success` pending state (#556, item 2) ---------------------
#
# Checkout's success_url always carries `?billing=success`; the mirror may
# not have seen the subscription's webhook yet. `checkout_pending` shows a
# "finishing" placeholder instead of a stale free/local-trial Subscribe form
# until the mirror catches up (`has_live_stripe_subscription` goes true), at
# which point the flag turns itself off and the page shows the real state.


class TestCheckoutPendingBillingSurfaces:
    @pytest.mark.parametrize("mirror", ["free", "local_trial"])
    def test_pending_param_shows_finishing_copy_and_hides_subscribe(
        self, client, mirror
    ):
        coach = _coach()
        if mirror == "local_trial":
            CoachSubscriptionFactory(
                coach=coach,
                status=CoachSubscription.Status.TRIALING,
                trial_end=timezone.now() + timedelta(days=10),
            )
        client.force_login(coach)
        for base in (URL, ROSTER_URL):
            body = client.get(f"{base}?billing=success").content.decode()
            assert "Finishing your subscription" in body
            assert 'action="/meso/billing/subscribe/"' not in body

    @pytest.mark.parametrize("mirror", ["free", "local_trial"])
    def test_without_the_param_the_same_coach_sees_the_subscribe_form(
        self, client, mirror
    ):
        coach = _coach()
        if mirror == "local_trial":
            CoachSubscriptionFactory(
                coach=coach,
                status=CoachSubscription.Status.TRIALING,
                trial_end=timezone.now() + timedelta(days=10),
            )
        client.force_login(coach)
        for url in (URL, ROSTER_URL):
            body = client.get(url).content.decode()
            assert "Finishing your subscription" not in body
            assert 'action="/meso/billing/subscribe/"' in body

    def test_pending_param_with_an_active_mirror_shows_the_normal_pro_line(
        self, client
    ):
        coach = _coach()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        client.force_login(coach)
        for base in (URL, ROSTER_URL):
            body = client.get(f"{base}?billing=success").content.decode()
            assert "Finishing your subscription" not in body
            assert "active athlete" in body
