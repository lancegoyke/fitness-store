"""S6 — billing, Phase 5: per-athlete suspension granularity on downgrade.

D6 lands a coach who drops below their active-athlete count on the free tier with
read access intact but edits/deliver frozen until they're back within the cap.
Phase 3 enforced that **coarsely** — the whole coach was frozen
(``can_edit(coach)``). This slice refines it **per athlete**: the coach keeps full
control of their *oldest* ``FREE_SEAT_LIMIT`` active relationships and only the
rest are soft-suspended (frozen, never deleted). Re-subscribing or ending a
relationship lifts the freeze. See ``docs/meso/billing-plan.md``.

Covered:

- ``access.suspended_athlete_ids`` — empty within the cap and for an active/comped
  coach; the active links beyond the oldest ``FREE_SEAT_LIMIT`` once a free/lapsed
  coach is over the cap.
- ``access.can_edit_plan`` — an individual plan is frozen only when *its*
  relationship is suspended.
- the endpoints: an over-limit coach can still patch / deliver / apply a batch for
  a kept (oldest) athlete, but is 402'd on a suspended one.
- the roster surfaces a per-athlete "Suspended" badge and a count.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.billing import access
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
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


def _aged_link(coach, days_ago):
    """An active ``CoachAthlete`` for ``coach`` whose ``created_at`` is back-dated.

    ``created_at`` is ``auto_now_add``, so a ``.update()`` (raw SQL, no auto-stamp)
    is the only way to set a deterministic relationship age — the ordering the
    oldest-kept rule turns on.
    """
    link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
    CoachAthlete.objects.filter(pk=link.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    link.refresh_from_db()
    return link


def _aged_plan(coach, days_ago):
    """A minimal individual plan (week → session → prescription) on an aged link."""
    rel = _aged_link(coach, days_ago)
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name="Back Squat")
    return rel, plan, cell


# ---------------------------------------------------------------------------
# access.suspended_athlete_ids — which active links are frozen
# ---------------------------------------------------------------------------


class TestSuspendedAthleteIds:
    def test_within_cap_no_suspensions(self):
        coach = UserFactory()  # no row → free, cap 1
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        assert access.suspended_athlete_ids(coach) == frozenset()

    def test_over_limit_suspends_all_but_oldest(self):
        coach = UserFactory()
        oldest = _aged_link(coach, days_ago=30)
        mid = _aged_link(coach, days_ago=20)
        newest = _aged_link(coach, days_ago=10)
        suspended = access.suspended_athlete_ids(coach)
        # The oldest FREE_SEAT_LIMIT (1) link is kept live; the rest are frozen.
        assert oldest.pk not in suspended
        assert suspended == frozenset({mid.pk, newest.pk})
        assert len(suspended) == 3 - CoachSubscription.FREE_SEAT_LIMIT

    def test_active_coach_no_suspensions(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        for _ in range(3):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.suspended_athlete_ids(sub.coach) == frozenset()

    def test_comped_coach_no_suspensions(self):
        coach = UserFactory()
        for _ in range(3):
            CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachSubscription.comp(coach)
        assert access.suspended_athlete_ids(coach) == frozenset()

    def test_lapsed_trial_suspends_newest(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),  # lapsed → free-equivalent
        )
        oldest = _aged_link(sub.coach, days_ago=9)
        newest = _aged_link(sub.coach, days_ago=2)
        assert access.suspended_athlete_ids(sub.coach) == frozenset({newest.pk})
        assert oldest.pk not in access.suspended_athlete_ids(sub.coach)

    def test_ended_links_are_not_suspended(self):
        # Only *active* links count toward seats, so an ended one is never a
        # suspension candidate — and doesn't push the coach over the cap.
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ENDED)
        assert access.is_over_limit(coach) is False
        assert access.suspended_athlete_ids(coach) == frozenset()


# ---------------------------------------------------------------------------
# access.can_edit_plan — the per-plan gate
# ---------------------------------------------------------------------------


class TestCanEditPlan:
    def test_kept_individual_plan_editable(self):
        coach = UserFactory()
        _, kept_plan, _ = _aged_plan(coach, days_ago=30)  # oldest → kept
        _aged_plan(coach, days_ago=1)  # newer → suspended, pushes over cap
        assert access.is_over_limit(coach) is True
        assert access.can_edit_plan(kept_plan) is True

    def test_suspended_individual_plan_not_editable(self):
        coach = UserFactory()
        _aged_plan(coach, days_ago=30)  # oldest → kept
        _, suspended_plan, _ = _aged_plan(coach, days_ago=1)  # newer → suspended
        assert access.can_edit_plan(suspended_plan) is False

    def test_within_cap_individual_plan_editable(self):
        coach = UserFactory()
        _, plan, _ = _aged_plan(coach, days_ago=5)  # only athlete → at cap
        assert access.is_over_limit(coach) is False
        assert access.can_edit_plan(plan) is True

    def test_active_coach_any_plan_editable(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        _, kept_plan, _ = _aged_plan(sub.coach, days_ago=30)
        _, newer_plan, _ = _aged_plan(sub.coach, days_ago=1)
        assert access.can_edit_plan(kept_plan) is True
        assert access.can_edit_plan(newer_plan) is True


# ---------------------------------------------------------------------------
# Endpoints — an over-limit coach edits the kept plan, is 402'd on a suspended one
# ---------------------------------------------------------------------------


def _patch_url(plan, presc):
    return reverse(
        "meso:api_prescription_patch", kwargs={"plan_id": plan.pk, "pk": presc.pk}
    )


def _over_limit_coach_two_plans():
    """A free coach over the cap with a kept (oldest) plan and a suspended one."""
    coach = UserFactory()
    _, kept, kept_presc = _aged_plan(coach, days_ago=30)
    _, suspended, suspended_presc = _aged_plan(coach, days_ago=1)
    return coach, (kept, kept_presc), (suspended, suspended_presc)


class TestPerAthleteEditEndpoints:
    def test_over_limit_can_patch_kept_plan(self, client):
        coach, (kept, kept_presc), _ = _over_limit_coach_two_plans()
        assert access.is_over_limit(coach) is True
        client.force_login(coach)
        resp = client.post(
            _patch_url(kept, kept_presc),
            data=json.dumps({"text": "9 x 9"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        kept_presc.refresh_from_db()
        assert kept_presc.text == "9 x 9"

    def test_over_limit_cannot_patch_suspended_plan(self, client):
        coach, _, (suspended, suspended_presc) = _over_limit_coach_two_plans()
        client.force_login(coach)
        resp = client.post(
            _patch_url(suspended, suspended_presc),
            data=json.dumps({"text": "9 x 9"}),
            content_type="application/json",
        )
        assert resp.status_code == 402
        assert resp.json()["over_limit"] is True
        suspended_presc.refresh_from_db()
        assert suspended_presc.text != "9 x 9"

    def test_over_limit_can_deliver_kept_plan(self, client):
        coach, (kept, _), _ = _over_limit_coach_two_plans()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": kept.pk})
        )
        assert resp.status_code != 402  # gate let it through (delivers the week)

    def test_over_limit_cannot_deliver_suspended_plan(self, client):
        coach, _, (suspended, _) = _over_limit_coach_two_plans()
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": suspended.pk})
        )
        assert resp.status_code == 402

    def test_over_limit_can_apply_batch_for_kept_plan(self, client):
        coach, (kept, _), _ = _over_limit_coach_two_plans()
        batch = AgentProposalBatchFactory(plan=kept, coach=coach)
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code != 402

    def test_over_limit_cannot_apply_batch_for_suspended_plan(self, client):
        coach, _, (suspended, _) = _over_limit_coach_two_plans()
        batch = AgentProposalBatchFactory(plan=suspended, coach=coach)
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 402
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.PENDING  # not applied


# ---------------------------------------------------------------------------
# Roster surface — per-athlete badge + count
# ---------------------------------------------------------------------------


class TestRosterSuspensionUI:
    def test_roster_marks_suspended_athlete(self, client):
        coach = UserFactory()
        kept_rel, _, _ = _aged_plan(coach, days_ago=30)
        suspended_rel, _, _ = _aged_plan(coach, days_ago=1)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        by_id = {a["id"]: a for a in resp.context["athletes"]}
        assert by_id[kept_rel.athlete_id]["status"] != "suspended"
        assert by_id[suspended_rel.athlete_id]["status"] == "suspended"
        assert b"Suspended" in resp.content

    def test_roster_no_suspended_badge_within_cap(self, client):
        coach = UserFactory()
        _aged_plan(coach, days_ago=5)  # one athlete → at cap
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert all(a["status"] != "suspended" for a in resp.context["athletes"])
        assert b"Suspended" not in resp.content

    def test_billing_state_reports_suspended_count(self, client):
        coach = UserFactory()
        _aged_plan(coach, days_ago=30)
        _aged_plan(coach, days_ago=2)
        _aged_plan(coach, days_ago=1)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.context["billing"]["suspended_count"] == 2
