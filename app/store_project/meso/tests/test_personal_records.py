"""Personal records (parity plan §6 "Later — Extensions" → PRs).

The first PR slice: derive-on-read best estimated-1RM *with provenance* from the
athlete's structured performed record (``LoggedSet``), and detect whether a
just-logged session beat the athlete's prior best. It rides on the same DONE-only,
unit-scoped Epley scan as ``one_rm.derive_one_rm_values`` — reusing
``epley_one_rm`` and the ``_exercise_key`` identity — but adds (a) which logged
set on which date produced the best, and (b) new-PR detection.

These tests pin: the Epley tie, best-per-lift with provenance, lift-identity
keying (catalog FK vs case-folded name), unit scoping, DONE-only, non-numeric
skipping, and ``new_records_in`` true/false/tie against the best EXCLUDING the
session under test.
"""

import datetime

import pytest
from django.utils import timezone

from store_project.meso import personal_records as pr
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.one_rm import epley_one_rm
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as build_presc

pytestmark = pytest.mark.django_db


# -- fixtures / helpers (mirror test_one_rm.py) ----------------------------


def make_session(athlete, *, coach=None, unit=Unit.KILOGRAMS, prescriptions=()):
    """A delivered-shape plan → week → session with the given prescription specs."""
    coach = coach or UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE, unit=unit)
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    week = WeekFactory(mesocycle=meso, index=1, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower")
    presc = [
        build_presc(session, order=i, **spec) for i, spec in enumerate(prescriptions)
    ]
    return plan, session, presc


def log_session(athlete, session, rows, *, status=SessionLog.Status.DONE, date=None):
    """A ``SessionLog`` + ``LoggedSet`` rows. ``rows``: (presc, set_no, reps, load, rpe)."""
    log = SessionLogFactory(
        session=session,
        athlete=athlete,
        status=status,
        date=date or timezone.localdate(),
    )
    for presc, set_number, reps, load, rpe in rows:
        LoggedSetFactory(
            session_log=log,
            prescription=presc,
            set_number=set_number,
            reps=reps,
            load=load,
            rpe=rpe,
        )
    return log


# -- personal_records: best-e1RM-per-lift with provenance ------------------


class TestPersonalRecords:
    def test_single_rep_e1rm_is_the_load(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "1", "140", "9")])
        records = pr.personal_records(athlete, unit=Unit.KILOGRAMS)
        record = records["name:back squat"]
        assert record.e1rm == epley_one_rm("140", "1")
        assert record.e1rm == 140

    def test_best_per_lift_carries_provenance(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        # 5×100 → 116.67 (loser); 3×110 → 121 (winner).
        log_session(
            athlete,
            session,
            [(squat, 1, "5", "100", "8")],
            date=datetime.date(2026, 1, 1),
        )
        best_log = log_session(
            athlete,
            session,
            [(squat, 1, "3", "110", "9")],
            date=datetime.date(2026, 1, 2),
        )
        winning_set = best_log.sets.get()
        record = pr.personal_records(athlete, unit=Unit.KILOGRAMS)["name:back squat"]
        assert record.e1rm == epley_one_rm("110", "3")
        assert record.reps == "3"
        assert record.load == "110"
        assert record.date == datetime.date(2026, 1, 2)
        assert record.logged_set_id == winning_set.id
        assert record.session_log_id == best_log.id
        assert record.unit == Unit.KILOGRAMS
        assert record.name == "Back Squat"

    def test_catalog_and_free_text_keys(self):
        from store_project.exercises.factories import ExerciseFactory

        athlete = UserFactory()
        ex = ExerciseFactory()
        _, session, (linked, free) = make_session(
            athlete,
            prescriptions=[
                {"name": "Whatever", "exercise": ex},
                {"name": "Back Squat"},
            ],
        )
        log_session(
            athlete,
            session,
            [(linked, 1, "1", "100", "9"), (free, 1, "1", "150", "9")],
        )
        records = pr.personal_records(athlete, unit=Unit.KILOGRAMS)
        assert set(records) == {f"id:{ex.pk}", "name:back squat"}
        assert records[f"id:{ex.pk}"].e1rm == 100
        assert records["name:back squat"].e1rm == 150

    def test_two_slots_same_name_collapse_to_one_pr(self):
        athlete = UserFactory()
        _, session, (a, b) = make_session(
            athlete,
            prescriptions=[{"name": "Back Squat"}, {"name": "back squat"}],
        )
        log_session(
            athlete,
            session,
            [(a, 1, "1", "120", "9"), (b, 1, "1", "150", "9")],
        )
        records = pr.personal_records(athlete, unit=Unit.KILOGRAMS)
        assert list(records) == ["name:back squat"]
        assert records["name:back squat"].e1rm == 150

    def test_unit_scopes_the_scan(self):
        athlete = UserFactory()
        _, kg_session, (kg_squat,) = make_session(
            athlete, unit=Unit.KILOGRAMS, prescriptions=[{"name": "Back Squat"}]
        )
        _, lb_session, (lb_squat,) = make_session(
            athlete, unit=Unit.POUNDS, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, kg_session, [(kg_squat, 1, "1", "150", "9")])
        log_session(athlete, lb_session, [(lb_squat, 1, "1", "300", "9")])
        kg = pr.personal_records(athlete, unit=Unit.KILOGRAMS)
        lb = pr.personal_records(athlete, unit=Unit.POUNDS)
        assert kg["name:back squat"].e1rm == 150
        assert lb["name:back squat"].e1rm == 300

    def test_pending_log_is_ignored(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "1", "200", "9")],
            status=SessionLog.Status.PENDING,
        )
        assert pr.personal_records(athlete, unit=Unit.KILOGRAMS) == {}

    def test_non_numeric_sets_are_skipped(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(athlete, session, [(squat, 1, "AMRAP", "BW", "9")])
        log_session(athlete, session, [(squat, 2, "", "", "")])
        assert pr.personal_records(athlete, unit=Unit.KILOGRAMS) == {}


# -- new_records_in: detection against the best EXCLUDING this session ------


class TestNewRecordsIn:
    def test_first_log_of_a_lift_is_a_new_pr(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log = log_session(athlete, session, [(squat, 1, "1", "160", "9")])
        records = pr.new_records_in(log)
        assert len(records) == 1
        rec = records[0]
        assert rec.key == "name:back squat"
        assert rec.name == "Back Squat"
        assert rec.value == 160
        assert rec.previous is None

    def test_beating_the_prior_best_is_a_new_pr(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "1", "150", "9")],
            date=datetime.date(2026, 1, 1),
        )
        log = log_session(
            athlete,
            session,
            [(squat, 1, "1", "160", "9")],
            date=datetime.date(2026, 1, 8),
        )
        records = pr.new_records_in(log)
        assert len(records) == 1
        assert records[0].value == 160
        assert records[0].previous == 150

    def test_tying_the_prior_best_is_not_a_new_pr(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "1", "150", "9")],
            date=datetime.date(2026, 1, 1),
        )
        log = log_session(
            athlete,
            session,
            [(squat, 1, "1", "150", "9")],
            date=datetime.date(2026, 1, 8),
        )
        assert pr.new_records_in(log) == []

    def test_losing_to_the_prior_best_is_not_a_new_pr(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log_session(
            athlete,
            session,
            [(squat, 1, "1", "150", "9")],
            date=datetime.date(2026, 1, 1),
        )
        log = log_session(
            athlete,
            session,
            [(squat, 1, "1", "140", "9")],
            date=datetime.date(2026, 1, 8),
        )
        assert pr.new_records_in(log) == []

    def test_detection_excludes_the_session_under_test(self):
        # The session's own set must not count as its own "prior best" — a lone
        # log is still a first-ever PR (previous None), not a tie against itself.
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log = log_session(athlete, session, [(squat, 1, "1", "150", "9")])
        records = pr.new_records_in(log)
        assert len(records) == 1
        assert records[0].previous is None

    def test_pending_session_yields_no_records(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log = log_session(
            athlete,
            session,
            [(squat, 1, "1", "160", "9")],
            status=SessionLog.Status.PENDING,
        )
        assert pr.new_records_in(log) == []

    def test_non_numeric_session_yields_no_records(self):
        athlete = UserFactory()
        _, session, (squat,) = make_session(
            athlete, prescriptions=[{"name": "Back Squat"}]
        )
        log = log_session(athlete, session, [(squat, 1, "AMRAP", "BW", "9")])
        assert pr.new_records_in(log) == []
