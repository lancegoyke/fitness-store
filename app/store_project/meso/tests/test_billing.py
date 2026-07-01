"""S6 — billing, Phase 1: the subscription spine (state + gating, no Stripe).

Meso is a multi-coach SaaS (B1); coaches pay (decision S6). Phase 1 is the pure
backend spine — a ``CoachSubscription`` mirror, a local (no-card) trial, a
``comped`` status, and the one gating accessor — with **no Stripe and nothing
enforced** yet (the invite/agent choke points wire these gates in at Phase 3).
See ``docs/meso/billing-plan.md``.

These tests cover:

- the ``CoachSubscription`` model: a fresh row defaults to ``free``; the
  ``is_active`` predicate over every status (free/past_due/canceled inactive;
  trialing/active/comped active) with a **lapsed local trial reading inactive
  before the sweep runs** (lazy expiry, mirroring the invite slice);
- the state machine: ``start_trial`` (free → trialing, ∼14-day clock, single-use),
  ``expire_trial`` (a past-due trial → free), ``comp`` (→ comped, idempotent),
  and the ``start_trial_for`` get-or-create entry point;
- ``billing/access.py``: ``is_active`` / ``active_seat_count`` /
  ``effective_seat_limit`` / ``can_add_athlete`` / ``can_use_agent`` — a coach
  with **no subscription row** gates exactly as free (existing coaches predate
  billing), a seat is an *active* ``CoachAthlete`` link, the free cap blocks the
  next athlete, and an active/trial/comped coach is unlimited;
- the **free-tier agent allowance** (S6 Phase 5 — added later): a free coach gets
  ``FREE_AGENT_ALLOWANCE`` agent runs / calendar month (counted from the
  ``AgentProposalBatch`` ledger), an active coach is unlimited;
- the seed: the demo coach is ``comped`` so the owner is never paywalled (D12).
"""

import math
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from store_project.meso.billing import access
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# The CoachSubscription model
# ---------------------------------------------------------------------------


class TestCoachSubscriptionModel:
    def test_fresh_row_defaults_to_free(self):
        sub = CoachSubscription.objects.create(coach=UserFactory())
        assert sub.status == CoachSubscription.Status.FREE
        assert sub.trial_end is None
        assert sub.stripe_subscription_id == ""
        assert sub.quantity == 0
        assert sub.is_active is False

    @pytest.mark.parametrize(
        "status,expected",
        [
            (CoachSubscription.Status.FREE, False),
            (CoachSubscription.Status.TRIALING, True),
            (CoachSubscription.Status.ACTIVE, True),
            (CoachSubscription.Status.PAST_DUE, False),
            (CoachSubscription.Status.CANCELED, False),
            (CoachSubscription.Status.COMPED, True),
        ],
    )
    def test_is_active_per_status(self, status, expected):
        # Give a live trial clock so a TRIALING row reads active (not lapsed).
        sub = CoachSubscriptionFactory(
            status=status, trial_end=timezone.now() + timedelta(days=7)
        )
        assert sub.is_active is expected

    def test_lapsed_trial_reads_inactive_before_the_sweep(self):
        """A trialing row whose clock has run out is inactive immediately.

        The status flip to ``free`` is the Phase-2 sweep's job, but the gate must
        be correct the instant the trial lapses — same lazy-expiry taste as the
        invite slice (``is_claimable = is_pending and not is_expired``).
        """
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        assert sub.is_trial_expired is True
        assert sub.is_active is False

    def test_active_status_is_not_treated_as_an_expired_trial(self):
        # is_trial_expired only ever fires for a TRIALING row.
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.ACTIVE, trial_end=None
        )
        assert sub.is_trial_expired is False
        assert sub.is_active is True

    def test_start_trial_from_free(self):
        sub = CoachSubscription.objects.create(coach=UserFactory())
        before = timezone.now()
        sub.start_trial()
        sub.refresh_from_db()
        assert sub.status == CoachSubscription.Status.TRIALING
        assert sub.is_active is True
        # The clock lands ~TRIAL_DAYS out.
        expected = before + timedelta(days=CoachSubscription.TRIAL_DAYS)
        assert abs((sub.trial_end - expected).total_seconds()) < 30

    def test_start_trial_rejects_non_free(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        with pytest.raises(InvalidTransition):
            sub.start_trial()

    def test_trial_is_single_use(self):
        """A coach who has already trialed (clock set) can't trial again.

        After a trial lapses the status returns to ``free`` but ``trial_end``
        stays set — so the same row can't re-arm a second free trial.
        """
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.FREE,
            trial_end=timezone.now() - timedelta(days=1),
        )
        with pytest.raises(InvalidTransition):
            sub.start_trial()

    def test_expire_trial_lapses_to_free(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        sub.expire_trial()
        sub.refresh_from_db()
        assert sub.status == CoachSubscription.Status.FREE
        assert sub.is_active is False
        # The clock is preserved, so the trial stays single-use.
        assert sub.trial_end is not None

    def test_expire_trial_rejects_a_live_trial(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=3),
        )
        with pytest.raises(InvalidTransition):
            sub.expire_trial()

    def test_expire_trial_rejects_a_non_trial(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        with pytest.raises(InvalidTransition):
            sub.expire_trial()

    def test_comp_creates_a_comped_row(self):
        coach = UserFactory()
        sub = CoachSubscription.comp(coach)
        assert sub.status == CoachSubscription.Status.COMPED
        assert sub.is_active is True
        assert CoachSubscription.objects.get(coach=coach).status == (
            CoachSubscription.Status.COMPED
        )

    def test_comp_is_idempotent_and_upserts(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
        again = CoachSubscription.comp(sub.coach)
        assert again.pk == sub.pk
        assert again.status == CoachSubscription.Status.COMPED
        assert CoachSubscription.objects.filter(coach=sub.coach).count() == 1

    def test_start_trial_for_get_or_creates_then_trials(self):
        coach = UserFactory()
        sub = CoachSubscription.start_trial_for(coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert CoachSubscription.objects.filter(coach=coach).count() == 1

    def test_one_subscription_per_coach(self):
        coach = UserFactory()
        CoachSubscription.objects.create(coach=coach)
        with pytest.raises(Exception):
            CoachSubscription.objects.create(coach=coach)


# ---------------------------------------------------------------------------
# billing/access.py — the gating accessors
# ---------------------------------------------------------------------------


class TestBillingAccessIsActive:
    def test_no_subscription_row_is_not_active(self):
        coach = UserFactory()
        assert access.is_active(coach) is False

    def test_free_is_not_active(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
        assert access.is_active(sub.coach) is False

    @pytest.mark.parametrize(
        "status",
        [
            CoachSubscription.Status.TRIALING,
            CoachSubscription.Status.ACTIVE,
            CoachSubscription.Status.COMPED,
        ],
    )
    def test_active_statuses_are_active(self, status):
        sub = CoachSubscriptionFactory(
            status=status, trial_end=timezone.now() + timedelta(days=7)
        )
        assert access.is_active(sub.coach) is True

    def test_lapsed_trial_is_not_active(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        assert access.is_active(sub.coach) is False

    def test_can_use_agent_comped_and_fresh_coaches_pass(self):
        # Under the flat plan (D14) only ``comped`` is unlimited; a fresh free coach
        # still passes the gate off their monthly allowance (see ``TestAgentAllowance``).
        comped = CoachSubscriptionFactory(status=CoachSubscription.Status.COMPED)
        fresh_free = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
        assert access.can_use_agent(comped.coach) is True
        assert access.can_use_agent(fresh_free.coach) is True


class TestAgentAllowance:
    """The metered agent allowance (S6 Phase 5; flat plan D14).

    Under the flat monthly Pro plan every tier is metered per calendar month except
    ``comped``: a free coach gets ``FREE_AGENT_ALLOWANCE`` runs, a trialing/active
    coach the larger ``PAID_AGENT_ALLOWANCE``, and only a comped coach is unlimited.
    A run is an ``AgentProposalBatch`` (the batch table is the ledger — no separate
    counter), so the count is "batches this coach started this month."
    """

    def _backdate(self, batch, when):
        # ``created_at`` is ``auto_now_add``; ``update`` bypasses it to plant a row
        # in a chosen month for the boundary tests.
        AgentProposalBatch.objects.filter(pk=batch.pk).update(created_at=when)

    def _bulk_batches(self, coach, plan, n):
        # Plant ``n`` runs for the coach cheaply (one query, one plan) — the paid
        # allowance is large, so a factory-per-row loop would be needlessly slow.
        AgentProposalBatch.objects.bulk_create(
            [
                AgentProposalBatch(coach=coach, plan=plan, instruction="x")
                for _ in range(n)
            ]
        )

    def test_fresh_free_coach_has_full_allowance(self):
        coach = UserFactory()  # no row → free
        assert access.agent_allowance(coach) == CoachSubscription.FREE_AGENT_ALLOWANCE
        assert access.agent_runs_this_month(coach) == 0
        assert (
            access.agent_runs_remaining(coach) == CoachSubscription.FREE_AGENT_ALLOWANCE
        )
        assert access.can_use_agent(coach) is True

    def test_paid_coach_gets_the_larger_allowance(self):
        # A trialing/active coach is metered too now — at the larger paid cap.
        for status in (
            CoachSubscription.Status.TRIALING,
            CoachSubscription.Status.ACTIVE,
        ):
            sub = CoachSubscriptionFactory(status=status)
            assert (
                access.agent_allowance(sub.coach)
                == CoachSubscription.PAID_AGENT_ALLOWANCE
            )
            assert (
                access.agent_runs_remaining(sub.coach)
                == CoachSubscription.PAID_AGENT_ALLOWANCE
            )
            assert access.can_use_agent(sub.coach) is True

    def test_paid_coach_gate_closes_at_the_paid_cap(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        self._bulk_batches(
            sub.coach, PlanFactory(), CoachSubscription.PAID_AGENT_ALLOWANCE
        )
        assert access.agent_runs_remaining(sub.coach) == 0
        assert access.can_use_agent(sub.coach) is False

    def test_runs_this_month_count_against_the_allowance(self):
        coach = UserFactory()
        AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        assert access.agent_runs_this_month(coach) == 2
        assert (
            access.agent_runs_remaining(coach)
            == CoachSubscription.FREE_AGENT_ALLOWANCE - 2
        )

    def test_can_use_agent_false_once_allowance_is_exhausted(self):
        coach = UserFactory()
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE):
            AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        assert access.agent_runs_remaining(coach) == 0
        assert access.can_use_agent(coach) is False

    def test_remaining_never_goes_negative(self):
        coach = UserFactory()
        for _ in range(CoachSubscription.FREE_AGENT_ALLOWANCE + 3):
            AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        assert access.agent_runs_remaining(coach) == 0

    def test_last_months_runs_do_not_count(self):
        coach = UserFactory()
        old = AgentProposalBatchFactory(coach=coach, plan=PlanFactory())
        last_month = timezone.now().replace(day=1) - timedelta(days=1)
        self._backdate(old, last_month)
        assert access.agent_runs_this_month(coach) == 0
        assert (
            access.agent_runs_remaining(coach) == CoachSubscription.FREE_AGENT_ALLOWANCE
        )

    def test_another_coachs_runs_do_not_count(self):
        coach = UserFactory()
        other = UserFactory()
        AgentProposalBatchFactory(coach=other, plan=PlanFactory())
        assert access.agent_runs_this_month(coach) == 0

    def test_comped_coach_is_unlimited_regardless_of_usage(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.COMPED)
        # Even past the paid cap, a comped coach has no meter.
        self._bulk_batches(
            sub.coach, PlanFactory(), CoachSubscription.PAID_AGENT_ALLOWANCE + 1
        )
        assert access.agent_allowance(sub.coach) is None
        assert access.agent_runs_remaining(sub.coach) == math.inf
        assert access.can_use_agent(sub.coach) is True


class TestBillingAccessSeats:
    def test_active_seat_count_counts_only_active_links(self):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(
            coach=coach, status=CoachAthlete.Status.PENDING_COACH_INVITE
        )
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ENDED)
        # A link where this user is the *athlete* must not count toward their seats.
        CoachAthleteFactory(athlete=coach, status=CoachAthlete.Status.ACTIVE)
        assert access.active_seat_count(coach) == 2

    def test_effective_seat_limit_free_is_the_free_cap(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
        assert access.effective_seat_limit(sub.coach) == (
            CoachSubscription.FREE_SEAT_LIMIT
        )

    def test_effective_seat_limit_no_row_is_the_free_cap(self):
        coach = UserFactory()
        assert access.effective_seat_limit(coach) == (CoachSubscription.FREE_SEAT_LIMIT)

    def test_effective_seat_limit_active_is_unlimited(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        assert access.effective_seat_limit(sub.coach) == math.inf

    def test_free_coach_under_the_cap_can_add(self):
        coach = UserFactory()  # no row → free, 0 seats, cap 1
        assert access.can_add_athlete(coach) is True

    def test_free_coach_at_the_cap_cannot_add(self):
        coach = UserFactory()
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT):
            CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        assert access.can_add_athlete(coach) is False

    def test_trialing_coach_over_the_free_cap_can_add(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=7),
        )
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 3):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.can_add_athlete(sub.coach) is True

    def test_comped_coach_is_unlimited(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.COMPED)
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 5):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.can_add_athlete(sub.coach) is True

    def test_lapsed_trial_over_the_cap_cannot_add(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 1):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.can_add_athlete(sub.coach) is False


# ---------------------------------------------------------------------------
# Seed — the demo coach is comped (D12)
# ---------------------------------------------------------------------------


class TestSeedCompsTheDemoCoach:
    def test_demo_coach_is_comped(self):
        email = "billing-coach@example.test"
        call_command("seed_meso_demo", coach_email=email)
        coach = User.objects.get(email=email)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.COMPED
        assert access.is_active(coach) is True

    def test_reseed_keeps_the_coach_comped(self):
        email = "billing-coach@example.test"
        call_command("seed_meso_demo", coach_email=email)
        call_command("seed_meso_demo", coach_email=email)
        coach = User.objects.get(email=email)
        assert CoachSubscription.objects.filter(coach=coach).count() == 1
        assert CoachSubscription.objects.get(coach=coach).status == (
            CoachSubscription.Status.COMPED
        )
