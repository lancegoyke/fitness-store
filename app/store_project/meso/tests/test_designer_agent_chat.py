"""Agent slice Phase 3 — the designer's agent-chat column goes live.

The designer's left/agent column was a canned keyword intent engine
(``detectIntent``/``applyIntent`` in ``static/js/meso.js``): it matched the
coach's text to one of four scripted edits and mutated the in-memory grid
directly, with no backend, DB, or LLM call. Phase 3 retires that engine and
wires the chat to the real proposal endpoint (``POST api/plan/<id>/agent/``,
shipped in Phase 1): the coach's message is POSTed, the returned batch is
rendered inline, and a link sends the coach to the review gate. Proposed changes
stay inert until applied there — the chat never mutates the program grid.

Phase 2 PR B moved this wiring from ``meso.js``/``designer.html`` to the React
island (``frontend/designer/src/hooks/useAgentChat.ts``,
``lib/agent.ts``, ``components/ChatPanel.tsx``) — these guard the refactor at
the (new) source level, same "no JS runner in Django" pattern as before, just
repointed at the island's TSX. The island's own behavior has real vitest
coverage (``useAgentChat.test.ts``, ``ChatPanel.test.tsx``). The endpoint
itself is covered end-to-end in ``test_agent_endpoint.py``.
"""

from pathlib import Path

import pytest
from django.urls import reverse

from store_project.meso.models import CoachSubscription
from store_project.meso.tests.test_designer_save import seed_plan

pytestmark = pytest.mark.django_db

DESIGNER_SRC = Path(__file__).resolve().parents[4] / "frontend" / "designer" / "src"


def read_island_source(*parts):
    return (DESIGNER_SRC.joinpath(*parts)).read_text()


def read_designer_template():
    path = Path(__file__).resolve().parents[2] / "templates" / "meso" / "designer.html"
    return path.read_text()


class TestCannedEngineRetired:
    def test_island_agent_chat_drops_the_intent_engine(self):
        hook = read_island_source("hooks", "useAgentChat.ts")
        lib = read_island_source("lib", "agent.ts")
        for symbol in ("detectIntent", "applyIntent", "dispatch("):
            assert symbol not in hook, f"{symbol} should be retired in Phase 3"
            assert symbol not in lib, f"{symbol} should be retired in Phase 3"

    def test_island_agent_chat_drops_the_fabricated_seed_thread(self):
        # The canned seed messages invented logged loads ("92.5 kg" off a
        # trap-bar deadlift) that were never persisted — retired with the engine.
        hook = read_island_source("hooks", "useAgentChat.ts")
        assert "92.5 kg" not in hook

    def test_designer_template_drops_intent_chip_wiring(self):
        html = read_designer_template()
        assert "c.intent" not in html
        assert "applyIntent" not in html


class TestRealAgentWiring:
    def test_island_posts_instructions_to_the_agent_endpoint(self):
        hook = read_island_source("hooks", "useAgentChat.ts")
        assert "sendInstruction" in hook
        # The chat hits the real Phase 1 endpoint rather than a local matcher.
        assert "/agent/" in hook

    def test_island_renders_the_returned_batch_and_review_link(self):
        lib = read_island_source("lib", "agent.ts")
        # The inline render reads the endpoint's batch shape.
        assert "review_url" in lib
        assert "summary" in lib

    def test_chat_panel_wires_chips_as_instructions(self):
        tsx = read_island_source("components", "ChatPanel.tsx")
        assert "onChip(c.label)" in tsx

    def test_chat_panel_renders_inline_changes_and_review_link(self):
        tsx = read_island_source("components", "ChatPanel.tsx")
        # Inline batch render: the per-change list + a link to the review gate.
        assert "m.changes" in tsx
        assert "reviewUrl" in tsx


class TestPhase4BackgroundJobWiring:
    """The chat kicks off a background job and polls its status (Phase 4)."""

    def test_island_polls_the_batch_status_endpoint(self):
        hook = read_island_source("hooks", "useAgentChat.ts")
        assert "pollBatch" in hook
        assert "status_url" in hook

    def test_island_handles_drafting_and_failed_states(self):
        lib = read_island_source("lib", "agent.ts")
        assert '"drafting"' in lib or "'drafting'" in lib
        assert '"failed"' in lib or "'failed'" in lib


class TestDesignerStillRendersChatColumn:
    def test_designer_page_renders_the_agent_column(self, client):
        plan, _, _ = seed_plan()
        # The AI agent is paid-only (S6 Phase 3, D4), so a coach iterating a plan
        # with the agent in these tests has full access — comp keeps the gate open.
        CoachSubscription.comp(plan.relationship.coach)
        client.force_login(plan.relationship.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        # The composer's server-side gate: a comped coach's flags payload
        # opens the composer state (ChatPanel branches on this client-side —
        # see ChatPanel.tsx, which carries the "Ask the agent..." copy itself).
        assert '"can_use_agent": true' in body

    def test_chat_panel_carries_the_agent_column_copy(self):
        tsx = read_island_source("components", "ChatPanel.tsx")
        assert "Agent" in tsx
        assert "Ask the agent to adjust the program" in tsx
