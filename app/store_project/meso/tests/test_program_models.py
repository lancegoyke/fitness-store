"""Phase 2 program schema: hierarchy, hybrid exercise link, scoping, archiving.

Companion to ``test_serializers.py`` (the round-trip). These cover the model
contract the serializer rests on: the ``Plan → … → ExercisePrescription``
hierarchy, the hybrid catalog FK (B4), the per-relationship scoped managers
(N2/D-a), and plan archiving when a relationship ends (D-c).
"""

import pytest

from store_project.exercises.factories import ExerciseFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def active_relationship(coach=None, athlete=None):
    link = CoachAthlete.invite(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    link.accept()
    return link


class TestHierarchy:
    def test_plan_exposes_coach_and_athlete(self):
        rel = active_relationship()
        plan = PlanFactory(relationship=rel)
        assert plan.coach == rel.coach
        assert plan.athlete == rel.athlete

    def test_full_chain_relates_back_to_plan(self):
        session = day(WeekFactory())
        cell = presc(session)
        assert session.week.mesocycle.plan == session.week.mesocycle.plan
        assert cell in session.cells()
        assert session in session.week.sessions.all()

    def test_weeks_ordered_by_index(self):
        meso = MesocycleFactory()
        WeekFactory(mesocycle=meso, index=3)
        WeekFactory(mesocycle=meso, index=1)
        WeekFactory(mesocycle=meso, index=2)
        assert [w.index for w in meso.weeks.all()] == [1, 2, 3]

    def test_prescriptions_ordered_by_order(self):
        session = day(WeekFactory())
        presc(session, order=2, name="b")
        presc(session, order=0, name="a")
        presc(session, order=1, name="ab")
        assert [p.name for p in session.cells()] == ["a", "ab", "b"]


class TestHybridExercise:
    def test_links_to_catalog_exercise(self):
        ex = ExerciseFactory(name="Back Squat")
        cell = presc(day(WeekFactory()), exercise=ex, name="Back Squat")
        assert cell.exercise == ex
        assert cell.is_catalog_linked

    def test_free_text_when_unlinked(self):
        cell = presc(day(WeekFactory()), exercise=None, name="Sled Push")
        assert cell.exercise is None
        assert not cell.is_catalog_linked
        assert cell.name == "Sled Push"


class TestPlanScoping:
    def test_for_coach_only_active_relationship_plans(self):
        coach = UserFactory()
        mine = PlanFactory(relationship=active_relationship(coach=coach))
        # Another coach's plan.
        PlanFactory(relationship=active_relationship())
        # A pending (not yet active) relationship for the same coach.
        pending = CoachAthlete.invite(coach=coach, athlete=UserFactory())
        PlanFactory(relationship=pending)
        assert list(Plan.objects.for_coach(coach)) == [mine]

    def test_for_athlete_spans_multiple_coaches(self):
        athlete = UserFactory()
        r1 = active_relationship(athlete=athlete)
        r2 = CoachAthlete.request(athlete=athlete, coach=UserFactory())
        r2.accept()
        PlanFactory(relationship=r1)
        PlanFactory(relationship=r2)
        assert Plan.objects.for_athlete(athlete).count() == 2

    def test_active_excludes_archived(self):
        rel = active_relationship()
        PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        PlanFactory(relationship=rel, status=Plan.Status.ARCHIVED)
        assert Plan.objects.active().count() == 1


class TestArchiveOnEnd:
    def test_ending_relationship_archives_its_plans(self):
        rel = active_relationship()
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        rel.end()
        plan.refresh_from_db()
        assert plan.status == Plan.Status.ARCHIVED

    def test_ending_leaves_other_coaches_plans_untouched(self):
        athlete = UserFactory()
        r1 = active_relationship(athlete=athlete)
        r2 = active_relationship(athlete=athlete)
        p1 = PlanFactory(relationship=r1, status=Plan.Status.ACTIVE)
        p2 = PlanFactory(relationship=r2, status=Plan.Status.ACTIVE)
        r1.end()
        p1.refresh_from_db()
        p2.refresh_from_db()
        assert p1.status == Plan.Status.ARCHIVED
        assert p2.status == Plan.Status.ACTIVE


class TestLogging:
    def test_session_log_holds_logged_sets(self):
        session = day(WeekFactory())
        cell = presc(session)
        log = SessionLogFactory(session=session)
        logged = LoggedSetFactory(session_log=log, prescription=cell, set_number=1)
        assert logged.session_log == log
        assert log.session == session
        assert logged in log.sets.all()
