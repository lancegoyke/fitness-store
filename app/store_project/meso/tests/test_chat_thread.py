"""Persisted designer chat thread — the conversation survives a reload.

The designer's agent chat went live in agent Phase 3, but the thread itself was
never persisted: the front end re-seeds ``messages`` to a single greeting on
every load. The proposals, though, *are* persisted — every coach turn is an
``AgentProposalBatch`` (``instruction`` = the coach's message, ``summary`` + the
``ProposedChange`` rows = the agent's reply, ``status``/``created_at`` = state and
order). So this slice reconstructs the thread from those batches rather than
adding a model: ``serialize_chat_thread(plan)`` expands the plan's batches into
the message shape the front end renders, the designer view injects it, and the
JS hydrates ``messages`` from it (falling back to the greeting when empty).

Phase 2 PR B moved the hydration/resume logic from ``meso.js`` to the React
island's ``DesignerRoot.tsx``/``hooks/useAgentChat.ts`` — the JS-wiring checks
below were repointed there (mirroring ``test_designer_agent_chat.py``); the
serializer + view seam is still covered end-to-end here against the rendered
response body (the hydration payload is still server-injected JSON either way).
"""

from pathlib import Path

import pytest
from django.urls import reverse

from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.serializers import serialize_chat_thread
from store_project.meso.tests.test_designer_save import seed_plan

pytestmark = pytest.mark.django_db

DESIGNER_SRC = Path(__file__).resolve().parents[4] / "frontend" / "designer" / "src"


def read_island_source(*parts):
    return (DESIGNER_SRC.joinpath(*parts)).read_text()


class TestSerializeChatThread:
    def test_empty_plan_has_no_messages(self):
        plan, _, _ = seed_plan()
        assert serialize_chat_thread(plan) == []

    def test_one_batch_yields_coach_then_agent(self):
        plan, _, _ = seed_plan()
        batch = AgentProposalBatchFactory(
            plan=plan,
            instruction="Lower Day 2 volume",
            summary="Trimmed Day 2 back-off sets.",
            status=AgentProposalBatch.Status.PENDING,
        )
        ProposedChangeFactory(batch=batch, title="Drop a back-off set")
        ProposedChangeFactory(batch=batch, title="Reduce accessory volume")

        thread = serialize_chat_thread(plan)

        assert len(thread) == 2
        coach, agent = thread
        assert coach["role"] == "coach"
        assert coach["text"] == "Lower Day 2 volume"
        assert agent["role"] == "agent"
        assert agent["text"] == "Trimmed Day 2 back-off sets."
        assert len(agent["changes"]) == 2
        assert agent["reviewUrl"] == reverse(
            "meso:review_batch", kwargs={"batch_id": batch.pk}
        )
        # Distinct, stable keys so Alpine's x-for never collides a coach/agent pair.
        assert coach["id"] != agent["id"]

    def test_changes_use_the_proposed_change_shape(self):
        plan, _, _ = seed_plan()
        batch = AgentProposalBatchFactory(plan=plan)
        ProposedChangeFactory(
            batch=batch,
            title="Swap to box squat",
            before="Back squat",
            after="Box squat",
        )

        agent = serialize_chat_thread(plan)[1]
        change = agent["changes"][0]
        # The inline render reads id/title/before/after — same shape the live
        # status endpoint returns, so a hydrated change renders identically.
        assert change["title"] == "Swap to box squat"
        assert change["before"] == "Back squat"
        assert change["after"] == "Box squat"
        assert "id" in change

    def test_thread_is_oldest_first(self):
        plan, _, _ = seed_plan()
        first = AgentProposalBatchFactory(plan=plan, instruction="First ask")
        second = AgentProposalBatchFactory(plan=plan, instruction="Second ask")

        thread = serialize_chat_thread(plan)

        coach_texts = [m["text"] for m in thread if m["role"] == "coach"]
        assert coach_texts == ["First ask", "Second ask"]
        # Sanity: each batch contributes a coach + an agent message, interleaved.
        assert [m["role"] for m in thread] == [
            "coach",
            "agent",
            "coach",
            "agent",
        ]
        assert first.created_at <= second.created_at

    def test_failed_batch_is_an_error_message(self):
        plan, _, _ = seed_plan()
        AgentProposalBatchFactory(
            plan=plan,
            instruction="Do something impossible",
            status=AgentProposalBatch.Status.FAILED,
            error="The agent request failed: boom",
        )

        agent = serialize_chat_thread(plan)[1]
        assert agent["role"] == "agent"
        assert agent["error"] is True
        assert agent["text"] == "The agent request failed: boom"
        assert not agent.get("changes")
        assert agent.get("reviewUrl") is None

    def test_batch_without_changes_has_no_review_link(self):
        plan, _, _ = seed_plan()
        AgentProposalBatchFactory(
            plan=plan,
            summary="No safe changes to make here.",
            status=AgentProposalBatch.Status.PENDING,
        )

        agent = serialize_chat_thread(plan)[1]
        assert agent["text"] == "No safe changes to make here."
        assert agent["changes"] == []
        assert agent["reviewUrl"] is None

    def test_batch_without_summary_or_changes_falls_back_to_a_note(self):
        plan, _, _ = seed_plan()
        AgentProposalBatchFactory(
            plan=plan, summary="", status=AgentProposalBatch.Status.PENDING
        )

        agent = serialize_chat_thread(plan)[1]
        assert agent["text"].strip(), (
            "a summary-less, change-less reply still says something"
        )
        assert agent["changes"] == []

    def test_drafting_batch_renders_a_neutral_note(self):
        # A batch still ``drafting`` at reload is surfaced as a neutral note, not
        # an error or a blank bubble.
        plan, _, _ = seed_plan()
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.DRAFTING)

        agent = serialize_chat_thread(plan)[1]
        assert agent["role"] == "agent"
        assert agent["text"].strip()
        assert not agent.get("error")
        assert not agent.get("changes")

    def test_drafting_batch_carries_a_poll_url_to_resume(self):
        # A run still in flight at render time carries its status URL so the
        # front-end can resume polling and replace the placeholder when it lands,
        # rather than leaving the thread stuck on the note.
        plan, _, _ = seed_plan()
        batch = AgentProposalBatchFactory(
            plan=plan, status=AgentProposalBatch.Status.DRAFTING
        )

        agent = serialize_chat_thread(plan)[1]
        assert agent["pollUrl"] == reverse(
            "meso:api_batch_status", kwargs={"batch_id": batch.pk}
        )
        # A resolved batch never carries a poll URL — nothing left to poll.
        resolved_plan, _, _ = seed_plan()
        AgentProposalBatchFactory(
            plan=resolved_plan, status=AgentProposalBatch.Status.PENDING
        )
        assert "pollUrl" not in serialize_chat_thread(resolved_plan)[1]

    def test_thread_is_plan_scoped(self):
        plan, _, _ = seed_plan()
        other_plan, _, _ = seed_plan()
        AgentProposalBatchFactory(plan=other_plan, instruction="On the other plan")

        assert serialize_chat_thread(plan) == []


class TestDesignerInjectsThread:
    def test_designer_injects_the_chat_thread(self, client):
        plan, _, _ = seed_plan()
        AgentProposalBatchFactory(plan=plan, instruction="Lower Day 2 volume")
        client.force_login(plan.relationship.coach)

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="meso-chat-thread"' in body
        assert "Lower Day 2 volume" in body

    def test_designer_injects_empty_thread_when_no_batches(self, client):
        plan, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

        body = resp.content.decode()
        assert 'id="meso-chat-thread"' in body
        # json_script renders an empty history as a literal empty array.
        assert ">[]</script>" in body

    def test_thread_only_carries_this_plans_batches(self, client):
        plan, _, _ = seed_plan()
        coach = plan.relationship.coach
        other_plan, _, _ = seed_plan(coach=coach)
        AgentProposalBatchFactory(plan=other_plan, instruction="Other plan ask")
        client.force_login(coach)

        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

        body = resp.content.decode()
        assert "Other plan ask" not in body


class TestIslandHydratesThread:
    def test_designer_root_reads_the_injected_thread(self):
        tsx = read_island_source("DesignerRoot.tsx")
        assert "meso-chat-thread" in tsx

    def test_designer_root_keeps_the_greeting_fallback(self):
        # An empty history still shows the orienting greeting.
        tsx = read_island_source("DesignerRoot.tsx")
        assert "Tell me how you'd like to adjust this plan" in tsx

    def test_island_resumes_polling_a_hydrated_drafting_run(self):
        # A run still drafting at load resumes via its poll URL so a thread
        # hydrated mid-run isn't left stuck on the placeholder — ported from
        # meso.js's resumeDrafting into useAgentChat's initialResumeUrl path.
        tsx = read_island_source("DesignerRoot.tsx")
        hook = read_island_source("hooks", "useAgentChat.ts")
        assert "pollUrl" in tsx
        assert "resumeDrafting" in hook  # breadcrumb comment to the ported behavior
        assert "initialResumeUrl" in hook
