"""First-time UX Phase 5 — designer & agent self-explanation.

The designer used to be a self-contained Alpine page that shipped a pile of
*prototype chrome*: a hardcoded fake athlete ("Maya Okonkwo" with invented
contraindications), a fabricated "Coach's programming style" block, and a
hardcoded macrocycle — all rendered over whatever real plan the coach opened. A
first-time coach also got no orientation: nothing said the grid autosaves, that
the agent only *proposes* (changes wait at the review gate), or that the phone
column is the athlete's real view.

Phase 5 fixed both:

- **Coachmarks.** A dismissible first-run note anchors the week grid. It
  shows until dismissed; the dismissal persists client-side (localStorage),
  like the athlete onboarding chrome. No server "seen" flag, no migration.
- **Agent self-explanation.** A persistent propose → review → apply note makes
  the review gate explicit for everyone (not just first-timers). The agent
  column originally also had its own dismissible coachmark and a chat greeting
  that both repeated this guidance — consolidated into this one note plus the
  greeting, which now just carries the "ask in plain words" examples.
- **Real chrome.** The fabricated left-rail athlete/programming-style/macrocycle
  is replaced with the *real* plan: the athlete's name + active
  contraindications (now carried in the serialized payload) and the real
  macrocycle phases.

Phase 2 PR B (designer-framework-plan.md, frontend/designer/CONTRACT.md) then
moved the whole UI from server-rendered Alpine to a React island
(``dist/designer.js``): the template itself no longer contains any of this
copy, so the render-level checks below were repointed to read the island's
TSX source instead of the rendered HTML body, mirroring the project's
existing "no JS runner" pattern of guarding client behavior at the source
level (previously against ``meso.js``, now against
``frontend/designer/src/``) — the island's own rendering behavior has real
coverage in its vitest suite (``MesoTable.test.tsx``, ``useCoachmarks.test.ts``,
``ChatPanel.test.tsx``, ``AthletePreview.test.tsx``; issue #455 phase A5
retired the one-week ``WeekGrid`` these checks originally read).
"""

from pathlib import Path

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ContraindicationFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.serializers import serialize_plan
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

DESIGNER_SRC = Path(__file__).resolve().parents[4] / "frontend" / "designer" / "src"


def read_island_source(*parts):
    return (DESIGNER_SRC.joinpath(*parts)).read_text()


def read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


def render_designer(client, plan):
    client.force_login(plan.relationship.coach)
    resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
    assert resp.status_code == 200
    return resp.content.decode()


class TestCoachmarksRender:
    """Both coachmarks render in the island source.

    The table one lives in MesoTable.tsx (issue #455 phase A4's re-authored
    copy; phase A5 deleted WeekGrid.tsx and its "grid" mark); the
    phone-preview one in AthletePreview.tsx — same dismiss plumbing, same
    first-run purpose.
    """

    def test_meso_table_renders_the_table_coachmark(self):
        tsx = read_island_source("components", "MesoTable.tsx")
        assert "The block table" in tsx  # table coachmark

    def test_athlete_preview_renders_the_phone_coachmark(self):
        tsx = read_island_source("components", "AthletePreview.tsx")
        assert "Preview as your athlete" in tsx
        assert 'dismissCoachmark?.("phone")' in tsx

    def test_table_coachmark_has_a_dismiss_control(self):
        tsx = read_island_source("components", "MesoTable.tsx")
        assert 'dismissCoachmark("table")' in tsx


class TestAgentSelfExplanation:
    """A persistent note makes the propose → review → apply loop explicit."""

    def test_chat_panel_renders_the_review_gate_note(self):
        tsx = read_island_source("components", "ChatPanel.tsx")
        assert "You review" in tsx
        assert "until you approve" in tsx


class TestStaticChromeReplaced:
    """The fabricated prototype chrome is gone; the real plan shows instead.

    Uses ``rel.create_plan(...)`` (the scaffolded, block-bearing plan every
    real coach gets), not a bare ``PlanFactory()``. Issue #455 phase A5 made
    ``#meso-grid-data`` (fed by ``serialize_mesocycle_grid``, which requires
    a mesocycle) the island's only hydration payload, retiring the separate
    ``#meso-plan-data`` (fed by ``serialize_plan``, unconditional) that used
    to carry athlete identity regardless of whether a block existed. A plan
    with no mesocycle at all doesn't hydrate the island (documented,
    accepted "shouldn't happen post-scaffold" edge case in
    ``MesoDesignerView.get_context_data``) — not the case these two tests
    are about, which is real athlete chrome vs. fabricated prototype chrome.
    """

    def test_real_athlete_identity_renders(self, client):
        athlete = UserFactory(name="Devon Reyes")
        rel = CoachAthleteFactory(athlete=athlete)
        plan = rel.create_plan(goal="Return to lifting")
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
        plan = rel.create_plan()
        body = render_designer(client, plan)
        assert "Maya" not in body
        assert "avoid deep knee flexion" not in body
        assert "programming style" not in body
        assert "14 mo trained" not in body
        assert "Base / GPP" not in body
        assert "Peak / Test" not in body


class TestCoachmarkSource:
    """The island exposes the (vitest-covered) dismiss API the grid wires to.

    ``lib/coachmarks.ts`` + ``hooks/useCoachmarks.ts`` were ``meso.js`` before
    Phase 2 PR B; the storage-facing primitives were
    renamed on the port (``coachmarkStorageKey``/``loadCoachmarks`` ->
    ``storageKey``/``readDismissed``), ``coachmarkVisible``/
    ``dismissCoachmark`` kept their names on ``useCoachmarks``.
    """

    def test_island_has_the_coachmark_api(self):
        lib = read_island_source("lib", "coachmarks.ts")
        hook = read_island_source("hooks", "useCoachmarks.ts")
        for symbol in ("storageKey", "readDismissed", "dismiss"):
            assert symbol in lib, f"lib/coachmarks.ts should define {symbol}"
        for symbol in ("coachmarkVisible", "dismissCoachmark"):
            assert symbol in hook, f"hooks/useCoachmarks.ts should define {symbol}"

    def test_meso_table_wires_coachmark_visibility(self):
        tsx = read_island_source("components", "MesoTable.tsx")
        assert 'coachmarkVisible("table")' in tsx

    def test_athlete_preview_wires_coachmark_visibility(self):
        tsx = read_island_source("components", "AthletePreview.tsx")
        assert 'coachmarkVisible?.("phone")' in tsx

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
