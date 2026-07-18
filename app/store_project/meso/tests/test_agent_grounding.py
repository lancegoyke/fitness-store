"""Agent slice Phase 4 — recent logged sessions feed the agent's grounding.

``build_context`` now includes a compact summary of the athlete's most recent
logged sessions (``recent_logs``) so a progression/deload proposal can anchor on
what the athlete actually did, not just the prescribed plan. The summary is
scoped to the plan's athlete and this plan's sessions, newest first, and capped.
"""

import datetime

import pytest

from store_project.meso.agent import service
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def test_context_plan_and_block_halves_agree_on_a_non_first_block():
    # §4b FIX 1 regression: ``context["block"]`` is the requested mesocycle,
    # but ``context["plan"]`` used to come from a bare ``serializers.
    # serialize_plan(plan)``, which resolves its OWN opening week via
    # ``current_week(plan)`` — the plan's earliest LIVE week, i.e. block 1.
    # For a block-2 run the model would see block 1's prescription ids sitting
    # right next to block 2's ``block`` payload; it could target one of those
    # block-1 ids, and validation would then silently drop the change as
    # outside ``batch.mesocycle`` — an edit the coach asked for that just
    # vanishes. Both halves must describe the SAME block.
    plan, session, cell1 = make_plan()  # block 1 (order=0), week 1, "Back Squat"
    meso2 = MesocycleFactory(plan=plan, order=1)
    week2 = WeekFactory(mesocycle=meso2, index=1)
    session2 = day(week2, day_number=1, name="Upper")
    cell2 = presc(session2, name="Bench Press")

    context = service.build_context(plan, meso2)

    block_ids = {
        ex["id"]
        for w in context["block"]["weeks"]
        for s in w["sessions"]
        for ex in s["exercises"]
    }
    plan_ids = {ex["id"] for s in context["plan"]["program"] for ex in s["exercises"]}
    assert cell2.pk in block_ids
    # This is the failure mode: if ``build_context`` reverts to a bare
    # ``serialize_plan(plan)``, ``plan_ids`` comes back as block 1's cells
    # instead and both of the assertions below flip.
    assert cell2.pk in plan_ids
    assert cell1.pk not in plan_ids


def test_context_includes_whole_block():
    # P4 whole-block grounding: the agent sees EVERY live week of the
    # grounded mesocycle — its full session/cell grid (numbers incl.
    # ``rest``) plus each week's volume/intensity — so it can program across
    # the block. (Which block gets grounded on is now the caller's explicit
    # ``mesocycle`` — the coach's viewed block, persisted on the batch —
    # docs/meso/remove-current-week-plan.md §4b; this test just pins the
    # whole-block shape, not the scoping itself.)
    plan, session, _ = make_plan()  # week 1 with a day + "Back Squat"
    week2 = WeekFactory(mesocycle=session.week.mesocycle, index=2)
    w2_session = day(week2, day_number=1, name="Lower")
    presc(w2_session, name="Front Squat")

    block = service.build_context(plan, plan.mesocycles.first())["block"]

    assert len(block["weeks"]) == 2
    for w in block["weeks"]:
        # Week meta exposes the progression levers.
        assert "volume" in w["week"]
        assert "intensity" in w["week"]
        # Each cell exposes its full numbers, including ``rest``.
        for s in w["sessions"]:
            for ex in s["exercises"]:
                assert "rest" in ex
    # Week 2's cell is visible — the agent sees beyond the first week.
    names = {
        ex["name"]
        for w in block["weeks"]
        for s in w["sessions"]
        for ex in s["exercises"]
    }
    assert "Front Squat" in names


def test_context_has_empty_recent_logs_without_any():
    plan, _, _ = make_plan()
    context = service.build_context(plan, plan.mesocycles.first())
    assert context["recent_logs"] == []


def test_context_includes_recent_logged_sets():
    plan, session, presc = make_plan()
    log = SessionLogFactory(
        session=session,
        athlete=plan.athlete,
        status=SessionLog.Status.DONE,
        date=datetime.date(2026, 6, 20),
    )
    LoggedSetFactory(session_log=log, prescription=presc, reps="5", load="100", rpe="8")

    context = service.build_context(plan, plan.mesocycles.first())

    logs = context["recent_logs"]
    assert len(logs) == 1
    assert logs[0]["status"] == "done"
    assert logs[0]["date"] == "2026-06-20"
    assert logs[0]["sets"][0]["exercise"] == "Back Squat"
    assert logs[0]["sets"][0]["load"] == "100"


def test_recent_logs_are_scoped_to_the_plans_athlete():
    plan, session, presc = make_plan()
    other_plan, other_session, _ = make_plan()
    # A log on a different athlete/plan must not leak into this plan's grounding.
    SessionLogFactory(session=other_session, athlete=other_plan.athlete)

    context = service.build_context(plan, plan.mesocycles.first())

    assert context["recent_logs"] == []


def test_recent_logs_are_capped_and_newest_first():
    plan, session, _ = make_plan()
    for day_num in range(1, 9):
        SessionLogFactory(
            session=session,
            athlete=plan.athlete,
            date=datetime.date(2026, 6, day_num),
        )

    logs = service.build_context(plan, plan.mesocycles.first())["recent_logs"]

    assert len(logs) == service.RECENT_LOG_LIMIT
    # Newest first.
    assert logs[0]["date"] == "2026-06-08"
