"""S6 — billing, Phase 3: enforcement + paywall UI.

Phase 1 stood up the ``CoachSubscription`` spine + the ``billing/access.py``
gating accessor; Phase 2 added Stripe Checkout / Portal / webhook + seat sync —
but **nothing was enforced**. Phase 3 wires the gates into the choke points and
ships the paywall UI. See ``docs/meso/billing-plan.md``.

These tests cover:

- the two new ``billing/access.py`` predicates: ``is_over_limit`` (the
  downgrade-landing state — a free coach holding more active athletes than the
  cap) and ``can_edit`` (its negation, the D6 edit/deliver freeze);
- the **seat gate** (``can_add_athlete``) at the three points a new active link
  is created — opening a coach email invite, accepting an athlete's request, and
  claiming an email invite — each blocked for a free coach at the cap, allowed
  for a coach with room;
- the **agent gate** (``can_use_agent``) at ``agent_propose`` — a free coach gets
  a 402 (no drafting batch), a paid/comped coach passes;
- the **D6 edit/deliver block** (``can_edit``) at the autosave / deliver / group
  endpoints — an over-limit coach gets a 402 (API) or a flashed redirect (group
  forms) and nothing is mutated; a within-cap free coach is unaffected;
- the **local trial-start endpoint** — a coach starts the no-card trial,
  single-use, non-coach bounced;
- the **paywall UI** — the roster billing card's CTAs + the designer's agent
  composer vs. upgrade CTA, both driven by the gates.
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
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import GroupMembershipFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _plan_with_prescription(coach, athlete=None):
    """A minimal individual plan the ``coach`` owns: week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach,
        athlete=athlete or UserFactory(),
        status=CoachAthlete.Status.ACTIVE,
    )
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
    meso = MesocycleFactory(plan=plan, order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = SessionFactory(week=week, day_number=1, name="Lower")
    presc = ExercisePrescriptionFactory(session=session, name="Back Squat")
    return plan, session, presc


# ---------------------------------------------------------------------------
# billing/access.py — the new D6 predicates
# ---------------------------------------------------------------------------


class TestAccessOverLimit:
    def test_free_over_cap_is_over_limit(self):
        coach = UserFactory()  # no row → free, cap 1
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 1):
            CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        assert access.is_over_limit(coach) is True
        assert access.can_edit(coach) is False

    def test_free_at_cap_is_not_over_limit(self):
        coach = UserFactory()
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT):
            CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        assert access.is_over_limit(coach) is False
        assert access.can_edit(coach) is True

    def test_no_row_no_athletes_can_edit(self):
        coach = UserFactory()
        assert access.is_over_limit(coach) is False
        assert access.can_edit(coach) is True

    def test_active_coach_is_never_over_limit(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 5):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.is_over_limit(sub.coach) is False
        assert access.can_edit(sub.coach) is True

    def test_lapsed_trial_over_cap_is_over_limit(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 1):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        assert access.is_over_limit(sub.coach) is True
        assert access.can_edit(sub.coach) is False


# ---------------------------------------------------------------------------
# The seat gate — can_add_athlete at the three choke points
# ---------------------------------------------------------------------------


class TestSeatGateCoachInvite:
    def test_free_coach_at_cap_cannot_open_invite(self, client):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # at cap
        client.force_login(coach)
        resp = client.post(reverse("meso:coach_invite"), data={"email": "new@x.com"})
        assert resp.status_code == 302
        assert CoachInvite.objects.filter(coach=coach).count() == 0

    def test_free_coach_with_room_can_open_invite(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)  # a coach with zero athletes
        client.force_login(coach)
        resp = client.post(reverse("meso:coach_invite"), data={"email": "new@x.com"})
        assert resp.status_code == 302
        assert CoachInvite.objects.filter(coach=coach, email="new@x.com").count() == 1

    def test_active_coach_over_free_cap_can_open_invite(self, client):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 2):
            CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        client.force_login(sub.coach)
        resp = client.post(reverse("meso:coach_invite"), data={"email": "new@x.com"})
        assert resp.status_code == 302
        assert CoachInvite.objects.filter(coach=sub.coach).count() == 1


class TestSeatGateInviteAccept:
    def test_free_coach_at_cap_cannot_accept_request(self, client):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # at cap
        link = CoachAthlete.request(athlete=UserFactory(), coach=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST

    def test_coach_with_room_accepts_request(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)  # zero athletes → room for one
        link = CoachAthlete.request(athlete=UserFactory(), coach=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:invite_accept", kwargs={"token": link.token}))
        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ACTIVE


class TestSeatGateInviteClaim:
    def test_free_coach_at_cap_blocks_claim(self, client):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # at cap
        invite, _ = CoachInvite.open_for(coach=coach, email="newbie@x.com")
        claimer = UserFactory()
        client.force_login(claimer)
        resp = client.post(
            reverse("meso:invite_claim", kwargs={"token": invite.token}),
            data={"action": "accept"},
        )
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.PENDING
        assert not CoachAthlete.objects.filter(coach=coach, athlete=claimer).exists()

    def test_coach_with_room_allows_claim(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        invite, _ = CoachInvite.open_for(coach=coach, email="newbie@x.com")
        claimer = UserFactory()
        client.force_login(claimer)
        resp = client.post(
            reverse("meso:invite_claim", kwargs={"token": invite.token}),
            data={"action": "accept"},
        )
        assert resp.status_code == 302
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert CoachAthlete.objects.filter(
            coach=coach, athlete=claimer, status=CoachAthlete.Status.ACTIVE
        ).exists()


# ---------------------------------------------------------------------------
# The agent gate — can_use_agent at agent_propose
# ---------------------------------------------------------------------------


def _agent_url(plan):
    return reverse("meso:api_plan_agent", kwargs={"plan_id": plan.pk})


class TestAgentGate:
    def test_free_coach_gets_402(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)  # no sub → free
        client.force_login(coach)
        resp = client.post(
            _agent_url(plan),
            data=json.dumps({"instruction": "Make it knee-safe."}),
            content_type="application/json",
        )
        assert resp.status_code == 402
        body = resp.json()
        assert body["ok"] is False
        assert body["upgrade"] is True

    def test_comped_coach_passes_the_gate(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        CoachSubscription.comp(coach)
        client.force_login(coach)
        resp = client.post(
            _agent_url(plan),
            data=json.dumps({"instruction": "Make it knee-safe."}),
            content_type="application/json",
        )
        # The billing gate is passed; the run then proceeds normally (here it hits
        # the no-API-key 503, never a 402). The point is the paywall let it through.
        assert resp.status_code != 402


# ---------------------------------------------------------------------------
# The D6 edit/deliver block — can_edit at the mutating endpoints
# ---------------------------------------------------------------------------


def _patch_url(plan, presc):
    return reverse(
        "meso:api_prescription_patch",
        kwargs={"plan_id": plan.pk, "pk": presc.pk},
    )


class TestEditGateIndividual:
    def test_over_limit_coach_cannot_patch(self, client):
        coach = UserFactory()
        plan, _, presc = _plan_with_prescription(coach)  # 1 active link
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # → over
        assert access.is_over_limit(coach) is True
        client.force_login(coach)
        resp = client.post(
            _patch_url(plan, presc),
            data=json.dumps({"sets": "9"}),
            content_type="application/json",
        )
        assert resp.status_code == 402
        assert resp.json()["over_limit"] is True
        presc.refresh_from_db()
        assert presc.sets != "9"

    def test_within_cap_free_coach_can_patch(self, client):
        coach = UserFactory()
        plan, _, presc = _plan_with_prescription(coach)  # exactly 1 = at cap
        assert access.is_over_limit(coach) is False
        client.force_login(coach)
        resp = client.post(
            _patch_url(plan, presc),
            data=json.dumps({"sets": "9"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        presc.refresh_from_db()
        assert presc.sets == "9"

    def test_over_limit_coach_cannot_deliver(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})
        )
        assert resp.status_code == 402

    def test_over_limit_coach_cannot_apply_pending_batch(self, client):
        # A batch drafted while paid, then a downgrade: applying it would mutate
        # the program, so the D6 freeze must block it (the agent gate alone only
        # stops *new* runs).
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # → over
        batch = AgentProposalBatchFactory(plan=plan, coach=coach)
        client.force_login(coach)
        resp = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )
        assert resp.status_code == 402
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.PENDING  # not applied


class TestEditGateGroup:
    def test_over_limit_coach_cannot_design_group(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        GroupMembershipFactory(group=group)
        GroupMembershipFactory(group=group)  # two active members → over the cap
        assert access.is_over_limit(coach) is True
        client.force_login(coach)
        resp = client.post(reverse("meso:group_design", kwargs={"pk": group.pk}))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:group", kwargs={"pk": group.pk})
        assert group.shared_plan() is None  # nothing created

    def test_over_limit_coach_cannot_group_deliver(self, client):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        GroupMembershipFactory(group=group)
        GroupMembershipFactory(group=group)
        client.force_login(coach)
        resp = client.post(reverse("meso:group_deliver", kwargs={"pk": group.pk}))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:group", kwargs={"pk": group.pk})


# ---------------------------------------------------------------------------
# The local trial-start endpoint
# ---------------------------------------------------------------------------


class TestTrialStartEndpoint:
    def test_coach_starts_trial(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:billing_start_trial"))
        assert resp.status_code == 302
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert access.is_active(coach) is True

    def test_trial_is_single_use(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.FREE,
            trial_end=timezone.now() - timedelta(days=1),  # already trialed
        )
        client.force_login(coach)
        resp = client.post(reverse("meso:billing_start_trial"))
        assert resp.status_code == 302
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.FREE  # unchanged

    def test_non_coach_cannot_start_trial(self, client):
        athlete = UserFactory()  # no coach profile / links / invites
        client.force_login(athlete)
        resp = client.post(reverse("meso:billing_start_trial"))
        assert resp.status_code == 302
        assert not CoachSubscription.objects.filter(coach=athlete).exists()

    def test_get_is_rejected(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:billing_start_trial"))
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Paywall UI — roster billing card + designer agent composer/CTA
# ---------------------------------------------------------------------------


class TestRosterBillingCard:
    def test_free_coach_sees_trial_cta(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert resp.context["billing"]["can_start_trial"] is True
        assert b"Start free trial" in resp.content

    def test_comped_coach_hides_trial_cta(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        CoachSubscription.comp(coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.context["billing"]["is_active"] is True
        assert resp.context["billing"]["can_start_trial"] is False
        assert b"Start free trial" not in resp.content


class TestDesignerAgentCta:
    def test_free_coach_sees_upgrade_cta(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        assert resp.context["can_use_agent"] is False
        assert b"Upgrade to use the agent" in resp.content

    def test_comped_coach_sees_composer(self, client):
        coach = UserFactory()
        plan, _, _ = _plan_with_prescription(coach)
        CoachSubscription.comp(coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.context["can_use_agent"] is True
        assert b"Upgrade to use the agent" not in resp.content
