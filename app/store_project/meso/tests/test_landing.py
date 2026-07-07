"""First-time UX — Phase 3: the front door (anonymous visitor + routing).

Phases 1–2 made a coach able to build an individual program and gave a fresh
coach a demo + empty-state teaching. Phase 3 fixes the *cold* visitor: ``/meso/``
was login-gated (``RosterView(LoginRequiredMixin)``), so anyone who'd never heard
of Meso met a bare login wall, and Meso was linked from nowhere on the main site.

This phase splits ``/meso/`` on auth — an anonymous visitor sees a real landing
page (what Meso is + two honest entry actions: log in as an athlete, or become a
coach), while an authenticated visitor keeps the post-#311 role routing (coach →
roster, anyone else → their training home). A single discreet "Meso" link in
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

import re

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import CoachSubscription
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

    def test_landing_names_the_free_trial(self, client):
        """The funnel sells the trial, not just the free tier (issue #416).

        Renders from ``CoachSubscription.TRIAL_DAYS`` so a future constant
        change can't leave the landing copy stale.
        """
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert "free trial" in body.lower()
        assert f"{CoachSubscription.TRIAL_DAYS} days" in body
        # The demo card's gate line points the limitation at the trial too.
        assert "held back" in body.lower()

    def test_landing_hero_has_demo_and_trial_ctas(self, client):
        """The hero carries its own CTA pair (issue #417).

        A cold visitor's first click shouldn't require scanning the cards
        below it.
        """
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert "Try the demo — no signup" in body
        assert "Start your free trial" in body

    def test_landing_orders_demo_before_athlete_login(self, client):
        """The cold visitor's entries lead; athlete login trails (issue #417).

        Ordering is the point of the issue, so assert it directly rather than
        just presence.
        """
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        demo_entry = body.index("Try the coach demo")
        athlete_entry = body.index("Training with a coach?")
        assert demo_entry < athlete_entry

    def test_landing_demo_card_no_signup_is_not_repeated(self, client):
        """The badge says "No signup"; the body used to repeat it (issue #418)."""
        resp = client.get(reverse("meso:roster"))
        assert resp.content.decode().count("No signup") == 1

    def test_landing_shows_the_flat_price(self, client):
        """A pricing signal renders from the shared constant, not a hardcoded string."""
        resp = client.get(reverse("meso:roster"))
        assert presenters.PRICE_SUMMARY in resp.content.decode()


# ---------------------------------------------------------------------------
# Hosted walkthrough video (issue #415 follow-up to #388)
# ---------------------------------------------------------------------------


class TestLandingDemoVideo:
    def test_video_section_renders_with_settings_url(self, client):
        """The <video> section renders from settings, not a hardcoded URL."""
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert "<video" in body
        assert f'<source src="{settings.MESO_DEMO_VIDEO_URL}"' in body
        assert 'type="video/mp4"' in body

    def test_poster_renders_from_settings(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert f'poster="{settings.MESO_DEMO_VIDEO_POSTER_URL}"' in body

    def test_preload_none_guards_page_weight(self, client):
        """The page-weight guarantee.

        An anonymous visit must not fetch the video unless the visitor
        presses play — the only thing that should load unconditionally is
        the poster.
        """
        resp = client.get(reverse("meso:roster"))
        assert 'preload="none"' in resp.content.decode()

    @override_settings(MESO_DEMO_VIDEO_URL="")
    def test_hidden_when_url_is_blank(self, client):
        """An empty override hides the section, not a broken/empty player.

        E.g. mid-refresh, before a first upload.
        """
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert "<video" not in body
        assert "See it in action" not in body


# ---------------------------------------------------------------------------
# Meta description + Open Graph tags (issue #418)
# ---------------------------------------------------------------------------


class TestLandingMeta:
    def test_meta_description_renders(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert '<meta name="description" content="' in body
        assert "coaches" in body.lower()

    def test_og_description_matches_meta_description_block(self, client):
        """og:description shares its block with the plain description tag."""
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert 'property="og:description"' in body

    def test_og_image_is_absolute_and_points_at_the_hero_shot(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        match = re.search(r'property="og:image" content="([^"]+)"', body)
        assert match is not None
        url = match.group(1)
        assert url.startswith("http://") or url.startswith("https://")
        assert url.endswith("meso-landing-designer.webp")

    def test_og_image_dimensions_and_twitter_card(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert '<meta property="og:image:width" content="1280" />' in body
        assert '<meta property="og:image:height" content="800" />' in body
        assert '<meta name="twitter:card" content="summary_large_image" />' in body

    def test_og_type_and_url(self, client):
        resp = client.get(reverse("meso:roster"))
        body = resp.content.decode()
        assert '<meta property="og:type" content="website" />' in body
        match = re.search(r'property="og:url" content="([^"]+)"', body)
        assert match is not None
        assert match.group(1).startswith("http")


# ---------------------------------------------------------------------------
# Discoverability — the main-site nav links into Meso
# ---------------------------------------------------------------------------


class TestMainSiteNavLink:
    def test_home_page_links_to_meso(self, client):
        resp = client.get(reverse("pages:home"))
        assert resp.status_code == 200
        assert reverse("meso:roster").encode() in resp.content
