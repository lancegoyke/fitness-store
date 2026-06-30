"""First-time UX Phase 5 — designer & agent self-explanation.

The designer is a self-contained Alpine page that, until now, shipped a pile of
*prototype chrome*: a hardcoded fake athlete ("Maya Okonkwo" with invented
contraindications), a fabricated "Coach's programming style" block, and a
hardcoded macrocycle — all rendered over whatever real plan the coach opened. A
first-time coach also got no orientation: nothing said the grid autosaves, that
the agent only *proposes* (changes wait at the review gate), or that the phone
column is the athlete's real view.

Phase 5 fixes both:

- **Coachmarks.** Three dismissible first-run notes anchor the designer's
  regions (week grid · agent · phone preview). They show until dismissed; the
  dismissal persists client-side (``meso.js`` localStorage), like the athlete
  onboarding chrome. No server "seen" flag, no migration.
- **Agent self-explanation.** A persistent propose → review → apply note makes
  the review gate explicit for everyone (not just first-timers).
- **Real chrome.** The fabricated left-rail athlete/programming-style/macrocycle
  is replaced with the *real* plan: the athlete's name + active
  contraindications (now carried in the serialized payload) and the real
  macrocycle phases.

There is no JS test runner wired into Django, so the dismiss logic is unit-tested
in ``frontend/meso.test.js`` and these guard the server seam: the serializer
carries the real identity, the template renders the coachmarks + note and no
longer renders the fabricated chrome, and ``meso.js`` exposes the dismiss API.
"""

from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.serializers import serialize_plan
from store_project.meso.tests.test_designer_save import seed_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def read_meso_js():
    path = finders.find("js/meso.js")
    assert path, "static js/meso.js must resolve"
    return Path(path).read_text()


def read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


def render_designer(client, plan):
    client.force_login(plan.relationship.coach)
    resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
    assert resp.status_code == 200
    return resp.content.decode()


class TestCoachmarksRender:
    """The three dismissible region coachmarks render for a real plan."""

    def test_designer_renders_all_three_coachmarks(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert "The week grid" in body  # grid coachmark
        assert "Talk to the agent" in body  # agent coachmark
        assert "Preview as your athlete" in body  # phone-preview coachmark

    def test_each_coachmark_has_a_dismiss_control(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        for key in ("grid", "agent", "phone"):
            assert f"dismissCoachmark('{key}')" in body


class TestAgentSelfExplanation:
    """A persistent note makes the propose → review → apply loop explicit."""

    def test_designer_renders_review_gate_note(self, client):
        plan, _, _ = seed_plan()
        body = render_designer(client, plan)
        assert "you review" in body
        assert "until you approve" in body


class TestStaticChromeReplaced:
    """The fabricated prototype chrome is gone; the real plan shows instead."""

    def test_real_athlete_identity_renders(self, client):
        athlete = UserFactory(name="Devon Reyes")
        rel = CoachAthleteFactory(athlete=athlete)
        plan = PlanFactory(relationship=rel, goal="Return to lifting")
        ContraindicationFactory(athlete=athlete, text="R shoulder impingement")
        body = render_designer(client, plan)
        assert "Devon Reyes" in body  # injected for the left rail to hydrate
        assert "R shoulder impingement" in body  # the real contraindication

    def test_fabricated_prototype_chrome_is_gone(self, client):
        # A real athlete with no recorded contraindications must not inherit the
        # prototype's invented ones (a safety-adjacent mislead), nor the fake
        # name / experience / programming-style / macrocycle chrome.
        athlete = UserFactory(name="Devon Reyes")
        rel = CoachAthleteFactory(athlete=athlete)
        plan = PlanFactory(relationship=rel)
        body = render_designer(client, plan)
        assert "Maya" not in body
        assert "avoid deep knee flexion" not in body
        assert "programming style" not in body
        assert "14 mo trained" not in body
        assert "Base / GPP" not in body
        assert "Peak / Test" not in body


class TestCoachmarkSource:
    """``meso.js`` exposes the (unit-tested) dismiss API the template wires to."""

    def test_meso_js_has_coachmark_api(self):
        js = read_meso_js()
        for symbol in (
            "coachmarkStorageKey",
            "loadCoachmarks",
            "coachmarkVisible",
            "dismissCoachmark",
        ):
            assert symbol in js, f"meso.js should define {symbol}"

    def test_template_wires_coachmark_visibility(self):
        html = read_designer_template()
        assert "coachmarkVisible('grid')" in html
        assert "coachmarkVisible('agent')" in html
        assert "coachmarkVisible('phone')" in html

    def test_template_drops_fabricated_left_rail(self):
        html = read_designer_template()
        # The macrocycle rail is wired to the real phases, not the hardcoded four.
        assert "Base / GPP" not in html
        assert "Coach&#8217;s programming style" not in html


class TestSerializerAthleteIdentity:
    """``serialize_plan`` carries the individual plan's real athlete identity."""

    def test_individual_plan_carries_real_athlete(self):
        athlete = UserFactory(name="Devon Reyes")
        rel = CoachAthleteFactory(athlete=athlete)
        plan = PlanFactory(relationship=rel, goal="Get strong")
        ContraindicationFactory(athlete=athlete, text="R shoulder impingement")
        ContraindicationFactory(athlete=athlete, text="ignored", active=False)
        data = serialize_plan(plan)
        assert data["athlete"]["name"] == "Devon Reyes"
        assert data["athlete"]["initials"] == "DR"
        assert data["athlete"]["goal"] == "Get strong"
        texts = [c["text"] for c in data["athlete"]["contraindications"]]
        assert texts == ["R shoulder impingement"]  # active only

    def test_group_plan_has_no_athlete_identity(self):
        group = MesoGroupFactory()
        plan = group.create_shared_plan()
        data = serialize_plan(plan)
        assert data["athlete"] is None
        assert data["group"] is not None
