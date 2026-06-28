"""Agent slice Phase 3 — the designer's agent-chat column goes live.

The designer's left/agent column was a canned keyword intent engine
(``detectIntent``/``applyIntent`` in ``static/js/meso.js``): it matched the
coach's text to one of four scripted edits and mutated the in-memory grid
directly, with no backend, DB, or LLM call. Phase 3 retires that engine and
wires the chat to the real proposal endpoint (``POST api/plan/<id>/agent/``,
shipped in Phase 1): the coach's message is POSTed, the returned batch is
rendered inline, and a link sends the coach to the review gate. Proposed changes
stay inert until applied there — the chat never mutates the program grid.

There is no JS test runner in this project, so these guard the refactor at the
source level (the canned engine is gone, the real wiring is present) alongside a
behavioral check that the designer renders a real plan's chat column. The
endpoint itself is covered end-to-end in ``test_agent_endpoint.py``.
"""

from pathlib import Path

import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse

from store_project.meso.tests.test_designer_save import seed_plan

pytestmark = pytest.mark.django_db


def read_meso_js():
    path = finders.find("js/meso.js")
    assert path, "static js/meso.js must resolve"
    return Path(path).read_text()


def read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


class TestCannedEngineRetired:
    def test_meso_js_drops_the_intent_engine(self):
        js = read_meso_js()
        for symbol in ("detectIntent", "applyIntent", "dispatch("):
            assert symbol not in js, f"{symbol} should be retired in Phase 3"

    def test_meso_js_drops_the_fabricated_seed_thread(self):
        # The canned seed messages invented logged loads ("92.5 kg" off a
        # trap-bar deadlift) that were never persisted — retired with the engine.
        js = read_meso_js()
        assert "92.5 kg" not in js

    def test_designer_template_drops_intent_chip_wiring(self):
        html = read_designer_template()
        assert "c.intent" not in html
        assert "applyIntent" not in html


class TestRealAgentWiring:
    def test_meso_js_posts_instructions_to_the_agent_endpoint(self):
        js = read_meso_js()
        assert "sendInstruction" in js
        # The chat hits the real Phase 1 endpoint rather than a local matcher.
        assert "/agent/" in js

    def test_meso_js_renders_the_returned_batch_and_review_link(self):
        js = read_meso_js()
        # The inline render reads the endpoint's batch shape.
        assert "review_url" in js
        assert "summary" in js

    def test_designer_template_wires_chips_as_instructions(self):
        html = read_designer_template()
        assert "onChip(c.label)" in html

    def test_designer_template_renders_inline_changes_and_review_link(self):
        html = read_designer_template()
        # Inline batch render: the per-change list + a link to the review gate.
        assert "m.changes" in html
        assert "reviewUrl" in html


class TestPhase4BackgroundJobWiring:
    """The chat kicks off a background job and polls its status (Phase 4)."""

    def test_meso_js_polls_the_batch_status_endpoint(self):
        js = read_meso_js()
        assert "pollBatch" in js
        assert "status_url" in js

    def test_meso_js_handles_drafting_and_failed_states(self):
        js = read_meso_js()
        assert '"drafting"' in js or "'drafting'" in js
        assert '"failed"' in js or "'failed'" in js


class TestDesignerStillRendersChatColumn:
    def test_designer_page_renders_the_agent_column(self, client):
        plan, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        # The agent column header + composer survive the rebuild.
        assert "Agent" in body
        assert "Ask the agent to adjust the program" in body
