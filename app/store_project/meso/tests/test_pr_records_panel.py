"""Phase 4d — the personal-records *panel* (the persistent "records book").

PR #1 (4c) surfaced the new-PR *event* at log time. This slice surfaces the
standing bests: a "Personal records" panel on the athlete's training home and on
the coach's athlete-profile, both fed by 4b's derive-on-read ``personal_records``
(best Epley e1RM per lift, with the winning set's provenance) through one shared
presenter helper and one ``_pr_list.html`` partial.

Unit is a per-PLAN property — there is no athlete-level unit preference — so each
host scopes to a single plan's unit (the most-recently-active one) rather than
pooling kg and lb. An athlete with no numeric-parseable best gets no panel (the
template guards on the row list), so a brand-new surface stays uncluttered.

Loads are chosen so Epley is exact: reps=5 → ``load * 7/6`` (120→140), reps=3 →
``load * 1.1`` (140→154), pinning the formatted labels without float noise.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.presenters import athlete_personal_records
from store_project.meso.presenters import coach_personal_records
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as make_presc

pytestmark = pytest.mark.django_db


def seed(*, coach=None, athlete=None):
    """A coach's owned, delivered session with two prescriptions (no log yet)."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory(name="Maya Okonkwo")
    rel = CoachAthleteFactory(coach=coach, athlete=athlete)
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=2, delivered_at=timezone.now())
    session = day(week, day_number=1, name="Lower")
    squat = make_presc(
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
    )
    rdl = make_presc(
        session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8"
    )
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        squat=squat,
        rdl=rdl,
    )


def log_done(s, *, squat=(), rdl=(), status=SessionLog.Status.DONE, when=None):
    """Log ``(reps, load, rpe)`` tuples against the seeded prescriptions."""
    sl = SessionLogFactory(
        session=s.session,
        athlete=s.athlete,
        status=status,
        date=when or date(2026, 6, 24),
    )
    for prescription, rows in ((s.squat, squat), (s.rdl, rdl)):
        for n, (reps, load, rpe) in enumerate(rows, start=1):
            LoggedSetFactory(
                session_log=sl,
                prescription=prescription,
                set_number=n,
                reps=reps,
                load=load,
                rpe=rpe,
            )
    return sl


# -- athlete home presenter ------------------------------------------------


class TestAthletePersonalRecordsPresenter:
    def test_lists_best_per_lift_alphabetical(self):
        s = seed()
        log_done(s, squat=[("5", "120", "8")], rdl=[("5", "120", "8")])
        panel = athlete_personal_records(s.athlete)
        assert panel["unit"] == "kg"
        rows = panel["rows"]
        assert [r["name"] for r in rows] == ["Box Squat", "RDL"]  # sorted by name
        assert rows[0]["e1rm"] == "140"  # 120 * 7/6
        assert rows[0]["reps"] == "5"
        assert rows[0]["load"] == "120"
        assert rows[0]["date"] == date(2026, 6, 24)

    def test_best_set_provenance_wins(self):
        s = seed()
        # A heavier low-rep single beats the lighter working set on e1RM.
        log_done(s, squat=[("5", "120", "8"), ("3", "140", "9")])
        rows = athlete_personal_records(s.athlete)["rows"]
        squat = next(r for r in rows if r["name"] == "Box Squat")
        assert squat["e1rm"] == "154"  # 140 * 1.1, beats 120 * 7/6 = 140
        assert squat["reps"] == "3"
        assert squat["load"] == "140"

    def test_pending_draft_still_shows_a_live_record(self):
        # 5a (plan §7): relaxed from DONE-only — the records panel is a LIVE,
        # derive-on-read view, so a PENDING draft's set counts. (Was
        # `test_pending_draft_is_not_a_record`, pinning the dropped DONE-only
        # gate; ``one_rm``'s *persisted* ``AthleteOneRm`` path is unaffected
        # and stays DONE-only — see ``test_one_rm.py``.)
        s = seed()
        log_done(s, squat=[("5", "120", "8")], status=SessionLog.Status.PENDING)
        rows = athlete_personal_records(s.athlete)["rows"]
        assert [r["name"] for r in rows] == ["Box Squat"]
        assert rows[0]["e1rm"] == "140"

    def test_empty_without_logs(self):
        s = seed()
        assert athlete_personal_records(s.athlete)["rows"] == []

    def test_empty_without_a_plan(self):
        athlete = UserFactory()
        assert athlete_personal_records(athlete) == {"rows": [], "unit": ""}


# -- coach profile presenter -----------------------------------------------


class TestCoachPersonalRecordsPresenter:
    def test_lists_records_in_plan_unit(self):
        s = seed()
        log_done(s, squat=[("5", "120", "8")])
        panel = coach_personal_records(s.rel)
        assert panel["unit"] == "kg"
        assert [r["name"] for r in panel["rows"]] == ["Box Squat"]
        assert panel["rows"][0]["e1rm"] == "140"

    def test_empty_without_a_plan(self):
        rel = CoachAthleteFactory(coach=UserFactory(), athlete=UserFactory())
        assert coach_personal_records(rel) == {"rows": [], "unit": ""}


# -- rendered surfaces -----------------------------------------------------


class TestAthleteHomeRendersRecords:
    def test_logged_athlete_sees_records_panel(self, client):
        s = seed()
        log_done(s, squat=[("5", "120", "8")])
        client.force_login(s.athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert "Personal records" in body

    def test_no_panel_without_records(self, client):
        s = seed()  # no log
        client.force_login(s.athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()
        assert "Personal records" not in body


class TestCoachProfileRendersRecords:
    def test_owning_coach_sees_records_panel(self, client):
        s = seed()
        log_done(s, squat=[("5", "120", "8")])
        client.force_login(s.coach)
        url = reverse("meso:athlete", kwargs={"pk": s.athlete.pk})
        body = client.get(url).content.decode()
        assert "Personal records" in body

    def test_no_panel_without_records(self, client):
        s = seed()  # no log
        client.force_login(s.coach)
        url = reverse("meso:athlete", kwargs={"pk": s.athlete.pk})
        body = client.get(url).content.decode()
        assert "Personal records" not in body
