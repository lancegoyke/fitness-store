"""Self-coaching (guided-tour Phase 0, ``docs/meso/demo-onboarding-tour-plan.md``).

A coach can put **themselves** on their roster — a real ``CoachAthlete`` link
with ``coach == athlete`` — to program for themselves. The one new billing rule:
that self-link is **never a paid seat** (``is_self`` is excluded from
``billable()``, exactly like ``is_demo``).

These tests cover:

- ``CoachAthlete.add_self``: straight to ``active`` (no invite dance), flagged
  ``is_self``, idempotent (one row per user), and a previously ended self-link
  reopens;
- the swapped check constraint: ``coach == athlete`` **iff** ``is_self`` — a
  bare same-user row is still rejected, and ``is_self`` can't be smuggled onto a
  two-party link; the invite/request state machine still refuses self-invites;
- billing neutrality: the self-link never moves ``active_seat_count``, never
  trips ``can_add_athlete`` / ``is_over_limit``, and the downgrade freeze
  (``suspended_athlete_ids``) never suspends it;
- the roster endpoint + rendering: POST ``roster_add_self`` creates the link
  (and the CoachProfile), re-posting is a no-op, the roster renders the
  coach-as-athlete row with a "You" badge, and the affordance hides once the
  self-link exists.
"""

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse

from store_project.meso.billing import access
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# CoachAthlete.add_self
# ---------------------------------------------------------------------------


class TestAddSelf:
    def test_creates_active_self_link(self):
        user = UserFactory()
        link = CoachAthlete.add_self(user)
        assert link.coach == user
        assert link.athlete == user
        assert link.is_self is True
        assert link.status == CoachAthlete.Status.ACTIVE
        assert link.responded_at is not None

    def test_idempotent_one_row_per_user(self):
        user = UserFactory()
        first = CoachAthlete.add_self(user)
        again = CoachAthlete.add_self(user)
        assert again.pk == first.pk
        assert CoachAthlete.objects.filter(coach=user, athlete=user).count() == 1

    def test_reopens_an_ended_self_link(self):
        user = UserFactory()
        link = CoachAthlete.add_self(user)
        link.end()
        reopened = CoachAthlete.add_self(user)
        assert reopened.pk == link.pk
        assert reopened.status == CoachAthlete.Status.ACTIVE
        assert reopened.ended_at is None
        assert reopened.is_self is True


# ---------------------------------------------------------------------------
# The same-user check constraint (coach == athlete iff is_self)
# ---------------------------------------------------------------------------


class TestSameUserConstraint:
    def test_same_user_without_is_self_rejected(self):
        user = UserFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            CoachAthlete.objects.create(
                coach=user,
                athlete=user,
                status=CoachAthlete.Status.ACTIVE,
                invited_by=CoachAthlete.InvitedBy.COACH,
            )

    def test_is_self_on_two_party_link_rejected(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            CoachAthleteFactory(is_self=True)

    def test_invite_and_request_still_refuse_self(self):
        # ``add_self`` is the only entry point to a self-link; the consent state
        # machine keeps rejecting a user on both sides.
        user = UserFactory()
        with pytest.raises(InvalidTransition):
            CoachAthlete.invite(coach=user, athlete=user)
        with pytest.raises(InvalidTransition):
            CoachAthlete.request(athlete=user, coach=user)


# ---------------------------------------------------------------------------
# Billing neutrality (the one new rule: never a paid seat)
# ---------------------------------------------------------------------------


class TestSelfLinkIsNotASeat:
    def test_self_link_does_not_move_seat_count(self):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach)  # one real, billable athlete
        assert access.active_seat_count(coach) == 1
        CoachAthlete.add_self(coach)
        assert access.active_seat_count(coach) == 1

    def test_self_link_ignores_the_free_cap(self):
        # A free coach at the seat cap can still add themselves — the self-link
        # is free, so there's no gate to trip and no over-limit state entered.
        coach = UserFactory()
        for _ in range(CoachSubscription.FREE_SEAT_LIMIT):
            CoachAthleteFactory(coach=coach)
        assert access.can_add_athlete(coach) is False
        CoachAthlete.add_self(coach)
        assert access.is_over_limit(coach) is False
        assert access.can_edit(coach) is True

    def test_downgrade_freeze_never_suspends_the_self_link(self):
        # An over-limit coach (paid seats held, then lapsed to free) has their
        # newest billable links suspended — the self-link is exempt like a demo.
        coach = UserFactory()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.FREE)
        self_link = CoachAthlete.add_self(coach)
        billable = [
            CoachAthleteFactory(coach=coach)
            for _ in range(CoachSubscription.FREE_SEAT_LIMIT + 1)
        ]
        assert access.is_over_limit(coach) is True
        suspended = access.suspended_athlete_ids(coach)
        assert self_link.pk not in suspended
        assert len(suspended) == len(billable) - CoachSubscription.FREE_SEAT_LIMIT


# ---------------------------------------------------------------------------
# The roster endpoint + rendering
# ---------------------------------------------------------------------------


class TestRosterAddSelf:
    def test_post_creates_link_and_coach_profile(self, client):
        user = UserFactory(name="Lena Ortiz")
        client.force_login(user)
        resp = client.post(reverse("meso:roster_add_self"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        link = CoachAthlete.objects.get(coach=user, athlete=user)
        assert link.is_self is True
        assert link.is_active
        # Adding yourself is an implicit "I'm coaching now" (mirrors demo_load).
        assert CoachProfile.objects.filter(user=user).exists()

    def test_repost_is_a_noop(self, client):
        user = UserFactory()
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        client.post(reverse("meso:roster_add_self"))
        assert CoachAthlete.objects.filter(coach=user, athlete=user).count() == 1

    def test_anonymous_is_bounced_to_login(self, client):
        resp = client.post(reverse("meso:roster_add_self"))
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url

    def test_roster_renders_self_row_with_you_badge(self, client):
        user = UserFactory(name="Lena Ortiz")
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Lena Ortiz" in body  # coach == athlete renders like any row
        assert ">You</span>" in body  # …with the self badge
        assert "Add yourself as an athlete" not in body  # affordance hides

    def test_roster_offers_add_self_until_linked(self, client):
        coach = UserFactory()
        CoachAthleteFactory(coach=coach)  # any coach surface, no self-link yet
        client.force_login(coach)
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Add yourself as an athlete" in body

    def test_athlete_profile_renders_for_self(self, client):
        # The coach's per-athlete record must survive coach == athlete.
        user = UserFactory(name="Lena Ortiz")
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        resp = client.get(reverse("meso:athlete", args=[user.pk]))
        assert resp.status_code == 200
        assert "Lena Ortiz" in resp.content.decode()


# ---------------------------------------------------------------------------
# An ended self-link: the roster affordance reopens it, not "re-invite"
# ---------------------------------------------------------------------------


class TestEndedSelfLink:
    def test_ending_a_self_link_offers_add_self_again(self, client):
        user = UserFactory(name="Nova Reyes")
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        link = CoachAthlete.objects.get(coach=user, athlete=user)

        client.post(reverse("meso:relationship_end", kwargs={"token": link.token}))

        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ENDED
        body = client.get(reverse("meso:roster")).content.decode()
        assert "Add yourself as an athlete" in body

    def test_ended_self_link_does_not_appear_in_relationship_history(self, client):
        user = UserFactory(name="Nova Reyes")
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        link = CoachAthlete.objects.get(coach=user, athlete=user)
        client.post(reverse("meso:relationship_end", kwargs={"token": link.token}))

        resp = client.get(reverse("meso:relationship_history"))

        assert resp.status_code == 200
        # No past athlete rows at all — the ended self-link is the only closed
        # link and it's excluded (the topnav renders the same name elsewhere on
        # this page, so assert on the row markup rather than a bare substring).
        assert resp.context["past"] == []
        body = resp.content.decode()
        assert '<div class="meso-row-name">Nova Reyes</div>' not in body

    def test_reinvite_on_an_ended_self_link_reopens_instead_of_500ing(self, client):
        # Defense-in-depth: a hand-crafted POST to the reinvite endpoint for an
        # ended self-link's token must not hit CoachAthlete.invite's "a user
        # cannot coach themselves" InvalidTransition — it should reopen instead.
        user = UserFactory(name="Nova Reyes")
        client.force_login(user)
        client.post(reverse("meso:roster_add_self"))
        link = CoachAthlete.objects.get(coach=user, athlete=user)
        client.post(reverse("meso:relationship_end", kwargs={"token": link.token}))
        link.refresh_from_db()

        resp = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": link.token})
        )

        assert resp.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.ACTIVE
        assert link.is_self is True
        assert link.ended_at is None
