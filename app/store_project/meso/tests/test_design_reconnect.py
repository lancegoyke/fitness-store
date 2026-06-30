"""Design-system unification — PR 3: Meso reconnection (final phase).

PRs 1–2 unified the *main* site on one token-driven look. Meso still read as a
separate website: its shell (``_meso_base.html``) carried no link back to the
rest of Mastering Fitness, and ``meso.css`` used its own purple-blue accent
(``oklch(0.56 0.14 258)``) instead of the site's steel-blue ``#31759d``.

This phase reconnects Meso:

- the **shared site nav** rides at the top of every coach-facing Meso shell page
  (brand → home; About / Store / Challenges / Coaching / Contact; auth on the
  right), so Meso reads as part of the same site, with the Meso workspace
  sub-header (``.meso-topnav``) kept below it;
- the phone-first **athlete PWA** surfaces (home, session logger, offline,
  invite-claim) suppress that nav to keep their installed-app feel;
- ``meso.css``, the standalone designer's inline tokens, and the PWA chrome
  (manifest ``theme_color``) all point at the shared steel-blue accent.

See ``docs/design-system-unification-plan.md`` (PR 3) and the validating spike
``docs/spikes/basecoat/meso.html``.
"""

import json
from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

SHARED_ACCENT = "#31759d"  # the site's steel-blue (base.css --accent)
OLD_ACCENT = "oklch(0.56 0.14 258)"  # the Meso-only accent being retired
# The Meso shell now reuses the one shared site nav (templates/_nav.html, styled
# by nav.css) rather than a Meso-only partial — design-system unification PR 3
# follow-up. Its header is the marker that only the shared site nav carries.
SITENAV_MARK = '<header class="nav"'


def make_coach():
    coach = UserFactory()
    CoachProfile.objects.create(user=coach)
    return coach


def seed_plan(coach=None, athlete=None):
    """A minimal owned, active plan with one current week → session → cell."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(),
        athlete=athlete or UserFactory(),
        status=CoachAthlete.Status.ACTIVE,
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = SessionFactory(week=week, day_number=1, name="Lower")
    ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return plan, session


# ---------------------------------------------------------------------------
# The shared site nav rides on the coach-facing Meso shell
# ---------------------------------------------------------------------------


class TestSharedSiteNav:
    def test_roster_carries_shared_site_nav(self, client):
        client.force_login(make_coach())
        body = client.get(reverse("meso:roster")).content.decode()
        assert SITENAV_MARK in body
        # the same top-level links as the main-site nav (templates/_nav.html)
        assert reverse("pages:home") in body
        assert reverse("products:store") in body
        assert reverse("challenges:challenge_filtered_list") in body
        assert reverse("pages:contact") in body

    def test_coaching_link_is_marked_active(self, client):
        client.force_login(make_coach())
        body = client.get(reverse("meso:roster")).content.decode()
        # Coaching is the current site section — the shared nav marks it active.
        expected = '<a class="link active" href="%s">Coaching</a>' % reverse(
            "meso:roster"
        )
        assert expected in body

    def test_authenticated_coach_sees_account_and_logout(self, client):
        client.force_login(make_coach())
        body = client.get(reverse("meso:roster")).content.decode()
        assert reverse("account_logout") in body
        assert reverse("users:profile") in body

    def test_anonymous_landing_reconnects_to_the_site(self, client):
        # The public front door also carries the shared nav (cold visitor → site),
        # showing the logged-out auth actions.
        body = client.get(reverse("meso:roster")).content.decode()
        assert SITENAV_MARK in body
        assert reverse("pages:home") in body
        assert reverse("account_signup") in body

    def test_shared_nav_does_not_leak_coach_only_surfaces(self, client):
        # The site nav is the *site* nav, not the coach workspace nav — it must
        # never advertise the Designer (kept in the .meso-topnav sub-header).
        body = client.get(reverse("meso:roster")).content.decode()  # anon landing
        # the site nav block itself carries no designer link
        assert SITENAV_MARK in body
        assert reverse("meso:designer") not in body


# ---------------------------------------------------------------------------
# The phone-first athlete PWA keeps its installed-app feel (no site nav)
# ---------------------------------------------------------------------------


class TestAthletePwaSuppressesSiteNav:
    def test_athlete_home_has_no_site_nav(self, client):
        plan, _ = seed_plan()
        client.force_login(plan.relationship.athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert SITENAV_MARK not in body

    def test_offline_page_has_no_site_nav(self, client):
        body = client.get(reverse("meso:offline")).content.decode()
        assert SITENAV_MARK not in body


# ---------------------------------------------------------------------------
# Every Meso accent surface points at the shared steel-blue
# ---------------------------------------------------------------------------


class TestSharedAccent:
    def test_meso_css_uses_the_shared_accent(self):
        path = finders.find("css/meso.css")
        assert path, "meso.css must be resolvable as a static file"
        css = Path(path).read_text()
        assert SHARED_ACCENT in css
        assert OLD_ACCENT not in css

    def test_designer_inline_tokens_use_the_shared_accent(self, client):
        # The standalone designer carries its own inline token block.
        plan, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        url = reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        body = client.get(url).content.decode()
        assert SHARED_ACCENT in body
        assert OLD_ACCENT not in body

    def test_pwa_theme_color_matches_the_shared_accent(self, client):
        data = json.loads(client.get(reverse("meso:manifest")).content)
        assert data["theme_color"] == SHARED_ACCENT
