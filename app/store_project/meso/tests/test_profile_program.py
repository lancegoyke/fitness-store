"""The athlete-profile program block — ``presenters.profile_program`` + render.

The profile's "Current block / adherence / macrocycle / latest session" card and
its left-rail goals shipped as a fully-built template fed dead placeholders
(``profile_athlete`` returned ``has_program=False``; the view hard-coded
``macrocycle=[]`` / ``results_summary=None``). Delivery + logging have existed
since the athlete slice, so the data is finally there. These pin the read-side
that lights the block up:

- it keys off the athlete's most recently *delivered* week (the same week the
  roster meter measures), spanning individual + group-delivery snapshots;
- ``has_program`` is gated on a *measurable* delivered week, so an athlete with
  nothing delivered honestly falls back to the create / in-progress empty state;
- ``status`` prefers the most actionable signal (needs_review > drafting >
  delivered); ``results_summary`` reuses the coach results scoring.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import presenters
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def make_plan(
    rel,
    *,
    blocks=("Accumulation", "Intensification", "Realization"),
    delivered_block=1,
    sessions=2,
    done=1,
    goal="Squat 405",
    status=Plan.Status.ACTIVE,
    source_group=None,
):
    """A plan under ``rel`` whose ``delivered_block``-th block holds the live week.

    Each block is a ``Mesocycle`` (``order``/``index`` ascending). Only the week
    in ``delivered_block`` is delivered (so it's unambiguously the latest), with
    ``sessions`` days, the first ``done`` of them logged *done* for the athlete.
    Returns ``(plan, delivered_week)``.
    """
    plan = PlanFactory(relationship=rel, status=status, goal=goal)
    if source_group is not None:
        plan.source_group = source_group
        plan.save(update_fields=["source_group"])
    delivered = None
    for i, name in enumerate(blocks):
        meso = MesocycleFactory(plan=plan, name=name, order=i, week_count=4)
        week = WeekFactory(mesocycle=meso, index=i + 1, delivered_at=None)
        if i == delivered_block:
            week.delivered_at = timezone.now()
            week.save(update_fields=["delivered_at"])
            for n in range(sessions):
                session = SessionFactory(
                    week=week, day_number=n + 1, name=f"Day {n + 1}"
                )
                if n < done:
                    SessionLogFactory(
                        session=session,
                        athlete=rel.athlete,
                        status=SessionLog.Status.DONE,
                    )
            delivered = week
    return plan, delivered


# -- empty state (nothing delivered) ---------------------------------------


class TestEmptyState:
    def test_no_plan_no_working_plan(self):
        rel = CoachAthleteFactory()
        program = presenters.profile_program(rel, None)
        assert program["athlete"]["has_program"] is False
        assert program["athlete"]["compliance"] is None
        assert program["athlete"]["status"] == ""
        assert program["athlete"]["goals"] == []
        assert program["macrocycle"] == []
        assert program["results_summary"] is None

    def test_working_plan_built_but_undelivered(self):
        # A plan exists (goal set) but no week is delivered → still the empty
        # state, but the goal surfaces so the left rail isn't blank pre-delivery.
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel, goal="Bench 315")
        MesocycleFactory(plan=plan, name="Base", order=0)
        program = presenters.profile_program(rel, plan)
        assert program["athlete"]["has_program"] is False
        assert program["athlete"]["goals"] == ["Bench 315"]

    def test_delivered_week_with_no_sessions_is_not_a_program(self):
        # A delivered week with nothing to measure (compliance None) must not
        # light up the block with a misleading meter.
        rel = CoachAthleteFactory()
        meso = MesocycleFactory(plan=PlanFactory(relationship=rel))
        WeekFactory(mesocycle=meso, delivered_at=timezone.now())  # no sessions
        program = presenters.profile_program(rel, None)
        assert program["athlete"]["has_program"] is False


# -- the lit-up block ------------------------------------------------------


class TestProgramBlock:
    def test_block_and_week_label_off_the_delivered_week(self):
        rel = CoachAthleteFactory()
        make_plan(rel, delivered_block=1)  # block index 1 → "Intensification", Wk 2
        athlete = presenters.profile_program(rel, None)["athlete"]
        assert athlete["has_program"] is True
        assert athlete["block"] == "Intensification"
        assert athlete["week"] == "Wk 2"

    def test_compliance_matches_the_delivered_week(self):
        rel = CoachAthleteFactory()
        make_plan(rel, sessions=2, done=1)  # 1 of 2 done → 50%
        athlete = presenters.profile_program(rel, None)["athlete"]
        assert athlete["compliance"] == 50

    def test_goal_from_the_delivered_plan(self):
        rel = CoachAthleteFactory()
        make_plan(rel, goal="Deadlift 500")
        athlete = presenters.profile_program(rel, None)["athlete"]
        assert athlete["goals"] == ["Deadlift 500"]

    def test_working_plan_goal_wins_over_delivered(self):
        # A coach who has since reset the goal on the working plan sees that one.
        rel = CoachAthleteFactory()
        plan, _ = make_plan(rel, goal="Old goal")
        plan.goal = "New goal"
        plan.save(update_fields=["goal"])
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["goals"] == ["New goal"]


# -- the macrocycle rail ---------------------------------------------------


class TestMacrocycle:
    def test_states_positioned_at_the_delivered_block(self):
        rel = CoachAthleteFactory()
        make_plan(
            rel,
            blocks=("B0", "B1", "B2", "B3"),
            delivered_block=1,
        )
        macro = presenters.profile_program(rel, None)["macrocycle"]
        assert [m["name"] for m in macro] == ["B0", "B1", "B2", "B3"]
        # Done before the live block, current on it, next immediately after,
        # future beyond.
        assert [m["state"] for m in macro] == ["done", "current", "next", "future"]
        assert macro[0]["weeks"] == "4 wk"


# -- the status badge ------------------------------------------------------


class TestStatus:
    def test_delivered_when_no_open_run(self):
        rel = CoachAthleteFactory()
        make_plan(rel)
        athlete = presenters.profile_program(rel, None)["athlete"]
        assert athlete["status"] == "delivered"
        assert athlete["status_label"] == "Delivered"

    def test_needs_review_with_a_pending_batch(self):
        rel = CoachAthleteFactory()
        plan, _ = make_plan(rel)
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.PENDING)
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["status"] == "needs_review"

    def test_drafting_with_an_in_flight_run(self):
        rel = CoachAthleteFactory()
        plan, _ = make_plan(rel)
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.DRAFTING)
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["status"] == "drafting"

    def test_needs_review_outranks_drafting(self):
        rel = CoachAthleteFactory()
        plan, _ = make_plan(rel)
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.DRAFTING)
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.PENDING)
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["status"] == "needs_review"

    def test_applied_batch_reads_delivered(self):
        # A run that's been reviewed and applied is no longer actionable.
        rel = CoachAthleteFactory()
        plan, _ = make_plan(rel)
        AgentProposalBatchFactory(plan=plan, status=AgentProposalBatch.Status.APPLIED)
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["status"] == "delivered"

    def test_another_athletes_pending_batch_does_not_leak(self):
        coach = UserFactory()
        rel = CoachAthleteFactory(coach=coach)
        other = CoachAthleteFactory(coach=coach)
        plan, _ = make_plan(rel)
        other_plan, _ = make_plan(other)
        AgentProposalBatchFactory(
            plan=other_plan, status=AgentProposalBatch.Status.PENDING
        )
        athlete = presenters.profile_program(rel, plan)["athlete"]
        assert athlete["status"] == "delivered"


# -- the latest-session card -----------------------------------------------


class TestResultsSummary:
    def test_summary_from_the_athletes_done_log(self):
        rel = CoachAthleteFactory()
        plan, week = make_plan(rel, sessions=2, done=0)  # log explicitly below
        session = week.sessions.order_by("day_number").first()
        ExercisePrescriptionFactory(session=session, name="Back Squat", sets="3")
        log = SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.DONE
        )
        for n in range(3):
            LoggedSetFactory(
                session_log=log,
                prescription=session.prescriptions.first(),
                set_number=n + 1,
            )
        summary = presenters.profile_program(rel, None)["results_summary"]
        assert summary is not None
        assert summary["completion"] == 100

    def test_none_when_nothing_logged(self):
        rel = CoachAthleteFactory()
        make_plan(rel, sessions=2, done=0)
        assert presenters.profile_program(rel, None)["results_summary"] is None

    def test_ignores_a_pending_draft(self):
        rel = CoachAthleteFactory()
        plan, week = make_plan(rel, sessions=2, done=0)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=rel.athlete, status=SessionLog.Status.PENDING
        )
        assert presenters.profile_program(rel, None)["results_summary"] is None

    def test_only_the_links_own_athlete(self):
        # A stray log under a different athlete (no DB constraint ties them) must
        # not surface as this athlete's latest session.
        rel = CoachAthleteFactory()
        plan, week = make_plan(rel, sessions=1, done=0)
        session = week.sessions.first()
        SessionLogFactory(
            session=session, athlete=UserFactory(), status=SessionLog.Status.DONE
        )
        assert presenters.profile_program(rel, None)["results_summary"] is None


# -- a group-delivery snapshot ---------------------------------------------


class TestGroupSnapshot:
    def test_block_lights_up_off_a_group_snapshot(self):
        # An athlete with no individual plan but a delivered group snapshot still
        # has a program; the goal falls back to the snapshot's.
        coach = UserFactory()
        rel = CoachAthleteFactory(coach=coach)
        group = MesoGroupFactory(coach=coach)
        make_plan(rel, goal="Group hypertrophy", source_group=group)
        assert rel.working_plan() is None  # the snapshot is not an editable plan
        athlete = presenters.profile_program(rel, rel.working_plan())["athlete"]
        assert athlete["has_program"] is True
        assert athlete["goals"] == ["Group hypertrophy"]
        assert athlete["status"] == "delivered"


# -- the rendered page -----------------------------------------------------


class TestProfileRender:
    def _coach_with_athlete(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        rel = CoachAthleteFactory(coach=coach)
        rel.athlete.name = "Maya Okonkwo"
        rel.athlete.save(update_fields=["name"])
        return coach, rel

    def test_delivered_program_renders_block_and_meter(self, client):
        coach, rel = self._coach_with_athlete()
        make_plan(rel, delivered_block=1, sessions=2, done=1)
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": rel.athlete_id}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Intensification" in body  # current block
        assert "Wk 2" in body
        assert "50%" in body  # adherence meter
        assert "Realization" in body  # macrocycle rail
        assert "Delivered" in body  # status badge

    def test_needs_review_surfaces_the_review_cta(self, client):
        coach, rel = self._coach_with_athlete()
        plan, _ = make_plan(rel)
        AgentProposalBatchFactory(
            plan=plan, coach=coach, status=AgentProposalBatch.Status.PENDING
        )
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": rel.athlete_id}))
        body = resp.content.decode()
        assert "Review agent changes" in body

    def test_undelivered_with_working_plan_shows_in_progress(self, client):
        coach, rel = self._coach_with_athlete()
        plan = PlanFactory(relationship=rel)
        MesocycleFactory(plan=plan, name="Base", order=0)
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": rel.athlete_id}))
        body = resp.content.decode()
        assert "not yet delivered" in body

    def test_no_program_shows_build_cta(self, client):
        coach, rel = self._coach_with_athlete()
        client.force_login(coach)
        resp = client.get(reverse("meso:athlete", kwargs={"pk": rel.athlete_id}))
        body = resp.content.decode()
        assert "No active program yet" in body
        assert "Build a program" in body
