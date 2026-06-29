"""S6 — billing, Phase 4: self-serve coach signup.

Phases 1–3 built the subscription spine, Stripe, and the gates/paywall — but a
``CoachProfile`` could still only be created by admin or the demo seed. Phase 4
ships the public funnel: a ``become a coach`` landing page and a ``start
coaching`` action that creates the ``CoachProfile`` for a logged-in user (and
optionally starts the no-card trial). Once a coach, plan choice (free / trial /
subscribe) is the existing Phase 3 roster billing card. See
``docs/meso/billing-plan.md``.

These tests cover:

- the **landing page** (``become_coach``) — public; an existing coach is bounced
  to the roster, an anonymous visitor sees the allauth signup CTA, a logged-in
  non-coach sees the ``start coaching`` form;
- the **start action** (``start_coaching``) — creates the ``CoachProfile``
  (idempotent), is POST-only + login-required, optionally starts the local trial
  with ``plan=trial`` (single-use-safe), and does *not* eagerly create a
  subscription row on the free path;
- the **entry point** — the athlete home surfaces a link into the funnel.
"""

import pytest
from django.urls import reverse

from store_project.meso.billing import access
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# The landing page — become_coach (public)
# ---------------------------------------------------------------------------


class TestBecomeCoachLanding:
    def test_anonymous_sees_signup_cta(self, client):
        resp = client.get(reverse("meso:become_coach"))
        assert resp.status_code == 200
        # Anonymous visitors are pointed at allauth signup, not the POST action
        # (a login redirect would come back as a GET the POST view rejects).
        assert reverse("account_signup").encode() in resp.content
        assert reverse("meso:start_coaching").encode() not in resp.content, (
            "the POST action form must not be offered to anonymous visitors"
        )

    def test_logged_in_non_coach_sees_start_form(self, client):
        user = UserFactory()  # no CoachProfile / links → a pure visitor
        client.force_login(user)
        resp = client.get(reverse("meso:become_coach"))
        assert resp.status_code == 200
        assert reverse("meso:start_coaching").encode() in resp.content

    def test_existing_coach_redirected_to_roster(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:become_coach"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")


# ---------------------------------------------------------------------------
# The start action — start_coaching
# ---------------------------------------------------------------------------


class TestStartCoaching:
    def test_creates_coach_profile_and_redirects(self, client):
        user = UserFactory()
        assert not CoachProfile.objects.filter(user=user).exists()
        client.force_login(user)
        resp = client.post(reverse("meso:start_coaching"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")
        assert CoachProfile.objects.filter(user=user).exists()

    def test_new_coach_can_now_load_the_roster(self, client):
        """The funnel's payoff: a brand-new coach reaches the coach surface."""
        user = UserFactory()
        client.force_login(user)
        client.post(reverse("meso:start_coaching"))
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200  # no longer bounced to athlete_home

    def test_idempotent_for_existing_coach(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.post(reverse("meso:start_coaching"))
        assert resp.status_code == 302
        assert CoachProfile.objects.filter(user=coach).count() == 1

    def test_free_path_creates_no_subscription_row(self, client):
        """The free tier is "no row" — signup must not eagerly create one."""
        user = UserFactory()
        client.force_login(user)
        client.post(reverse("meso:start_coaching"))
        assert not CoachSubscription.objects.filter(coach=user).exists()

    def test_plan_trial_starts_trial(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.post(reverse("meso:start_coaching"), data={"plan": "trial"})
        assert resp.status_code == 302
        assert CoachProfile.objects.filter(user=user).exists()
        sub = CoachSubscription.objects.get(coach=user)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert access.is_active(user) is True

    def test_plan_trial_already_trialed_is_safe(self, client):
        """A user who already trialed (e.g. was a coach before) gets no error."""
        from datetime import timedelta

        from django.utils import timezone

        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        CoachSubscription.objects.create(
            coach=coach,
            status=CoachSubscription.Status.FREE,
            trial_end=timezone.now() - timedelta(days=1),  # already used
        )
        client.force_login(coach)
        resp = client.post(reverse("meso:start_coaching"), data={"plan": "trial"})
        assert resp.status_code == 302
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.FREE  # unchanged, no crash

    def test_anonymous_redirected_to_login(self, client):
        resp = client.post(reverse("meso:start_coaching"))
        assert resp.status_code == 302
        assert reverse("account_login") in resp.url
        assert not CoachProfile.objects.exists()

    def test_get_is_rejected(self, client):
        user = UserFactory()
        client.force_login(user)
        resp = client.get(reverse("meso:start_coaching"))
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# The entry point — the athlete home links into the funnel
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_athlete_home_links_to_become_coach(self, client):
        user = UserFactory()  # a non-coach lands on the athlete home
        client.force_login(user)
        resp = client.get(reverse("meso:athlete_home"))
        assert resp.status_code == 200
        assert reverse("meso:become_coach").encode() in resp.content

    def test_athlete_who_is_already_coach_still_fine(self, client):
        """Sanity: a user coaching others can still view their own training home."""
        athlete_coach = UserFactory()
        pupil = UserFactory()
        CoachAthlete.objects.create(
            coach=athlete_coach,
            athlete=pupil,
            status=CoachAthlete.Status.ACTIVE,
        )
        client.force_login(athlete_coach)
        resp = client.get(reverse("meso:athlete_home"))
        assert resp.status_code == 200
