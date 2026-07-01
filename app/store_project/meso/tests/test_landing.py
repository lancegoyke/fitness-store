"""First-time UX — Phase 3: the front door (anonymous visitor + routing).

Phases 1–2 made a coach able to build an individual program and gave a fresh
coach a demo + empty-state teaching. Phase 3 fixes the *cold* visitor: ``/meso/``
was login-gated (``RosterView(LoginRequiredMixin)``), so anyone who'd never heard
of Meso met a bare login wall, and Meso was linked from nowhere on the main site.

This phase splits ``/meso/`` on auth — an anonymous visitor sees a real landing
page (what Meso is + two honest entry actions: log in as an athlete, or become a
coach), while an authenticated visitor keeps the post-#311 role routing (coach →
roster, anyone else → their training home). A single discreet "Coaching" link in
the main-site nav makes Meso discoverable at all. See
``docs/archive/meso/first-time-ux-plan.md`` (Phase 3).

These tests cover:

- the **front door** (``/meso/``) — anonymous → landing (not a login bounce);
  coach → roster (200, no redirect); a pure athlete → ``/meso/me/``;
- the **landing** — the two entry actions (athlete login carrying ``?next`` back
  to the training home, and the become-a-coach funnel) both render;
- the **main-site nav link** — the public home page links into ``/meso/`` so the
  front door is reachable without already knowing the URL.
"""

import pytest
from django.urls import reverse

from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# The front door — /meso/ splits on auth
# ---------------------------------------------------------------------------


class TestFrontDoor:
    def test_anonymous_sees_landing_not_login(self, client):
        """A cold visitor gets the landing page, not a bounce to /accounts/login/."""
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        body = resp.content.decode()
        # Two honest entry actions live on the landing.
        assert reverse("meso:become_coach") in body  # coach path
        assert reverse("account_login") in body  # athlete path (log in)

    def test_landing_athlete_cta_returns_to_training_home(self, client):
        """The athlete login CTA carries ?next back to their training home."""
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert reverse("meso:athlete_home") in resp.content.decode()

    def test_coach_sees_roster(self, client):
        coach = UserFactory()
        CoachProfile.objects.create(user=coach)
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200  # the roster renders, not redirected away
        assert b"Roster" in resp.content

    def test_pure_athlete_routed_to_training_home(self, client):
        """A logged-in non-coach is still routed off the coach surface (post-#311)."""
        athlete = UserFactory()  # no CoachProfile / coach-side link / sent invite
        client.force_login(athlete)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:athlete_home")

    def test_coach_via_link_only_sees_roster(self, client):
        """A coach by relationship (no CoachProfile) still reaches the roster."""
        coach = UserFactory()
        CoachAthlete.objects.create(
            coach=coach,
            athlete=UserFactory(),
            status=CoachAthlete.Status.ACTIVE,
        )
        client.force_login(coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
        assert b"Roster" in resp.content


# ---------------------------------------------------------------------------
# The landing page content
# ---------------------------------------------------------------------------


class TestLandingContent:
    def test_landing_explains_meso(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert "Meso" in body
        # Both audiences are addressed.
        assert "athlete" in body.lower()
        assert "coach" in body.lower()

    def test_landing_does_not_show_coach_nav(self, client):
        """A public page must not advertise the logged-in coach surfaces."""
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        # The Designer is a coach-only surface; the anonymous landing's topnav
        # must not link to it.
        assert reverse("meso:designer") not in body


# ---------------------------------------------------------------------------
# Discoverability — the main-site nav links into Meso
# ---------------------------------------------------------------------------


class TestMainSiteNavLink:
    def test_home_page_links_to_meso(self, client):
        resp = client.get(reverse("pages:home"))
        assert resp.status_code == 200
        assert reverse("meso:roster").encode() in resp.content
