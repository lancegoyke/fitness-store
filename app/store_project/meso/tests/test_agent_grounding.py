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
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_agent_validation import make_plan

pytestmark = pytest.mark.django_db


def test_context_has_empty_recent_logs_without_any():
    plan, _, _ = make_plan()
    context = service.build_context(plan)
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

    context = service.build_context(plan)

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

    context = service.build_context(plan)

    assert context["recent_logs"] == []


def test_recent_logs_are_capped_and_newest_first():
    plan, session, _ = make_plan()
    for day in range(1, 9):
        SessionLogFactory(
            session=session,
            athlete=plan.athlete,
            date=datetime.date(2026, 6, day),
        )

    logs = service.build_context(plan)["recent_logs"]

    assert len(logs) == service.RECENT_LOG_LIMIT
    # Newest first.
    assert logs[0]["date"] == "2026-06-08"
