"""Athlete slice Phase 1 — the athlete's read surface.

The first logged-in surface for an *athlete* (distinct from the coach's view of
an athlete at ``/meso/athlete/<uuid>/``):

- ``/meso/me/`` lists the athlete's active-coach plans, each with its latest
  delivered week and that week's sessions (marked done/pending from the
  athlete's own ``SessionLog``);
- ``/meso/me/session/<id>/`` shows one delivered session's prescribed exercises,
  read-only.

These tests pin the **scoping contract** (see ``docs/archive/meso/athlete-plan.md``):
an athlete sees only plans across their *active* coaches, only **delivered**
weeks, and never another athlete's data. Delivery — not plan status — is the
publish gate; an undelivered week is invisible even to its own athlete.
"""

from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(
    *,
    coach=None,
    athlete=None,
    delivered=True,
    link_status=CoachAthlete.Status.ACTIVE,
    plan_status=Plan.Status.ACTIVE,
    session_name="Lower",
):
    """A minimal plan → (optionally delivered) week → session → prescription."""
    coach = coach or UserFactory()
    athlete = athlete or UserFactory()
    rel = CoachAthleteFactory(coach=coach, athlete=athlete, status=link_status)
    plan = PlanFactory(relationship=rel, title="Hypertrophy Block", status=plan_status)
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(
        mesocycle=meso,
        index=2,
        is_current=True,
        delivered_at=timezone.now() if delivered else None,
    )
    session = SessionFactory(week=week, day_number=1, name=session_name, bias="Quad")
    presc = ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        meso=meso,
        week=week,
        session=session,
        presc=presc,
    )


HOME = reverse("meso:athlete_home")


def session_url(session):
    return reverse("meso:athlete_session", kwargs={"pk": session.pk})


# -- athlete home ----------------------------------------------------------


class TestAthleteHome:
    def test_requires_login(self, client):
        resp = client.get(HOME)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_lists_delivered_session(self, client):
        s = seed(session_name="Lower")
        client.force_login(s.athlete)
        resp = client.get(HOME)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Hypertrophy Block" in body
        assert "Lower" in body
        # The session links through to its detail page.
        assert session_url(s.session) in body

    def test_shows_coach_name(self, client):
        s = seed()
        s.coach.name = "Coach Lance"
        s.coach.save(update_fields=["name"])
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "Coach Lance" in body

    def test_hides_undelivered_week(self, client):
        s = seed(delivered=False, session_name="SecretLower")
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        # The plan card may show (awaiting delivery) but its sessions must not.
        assert "SecretLower" not in body
        assert session_url(s.session) not in body

    def test_excludes_other_athletes_plan(self, client):
        mine = seed(session_name="MyLower")
        theirs = seed(session_name="TheirLower")
        client.force_login(mine.athlete)
        body = client.get(HOME).content.decode()
        assert "MyLower" in body
        assert "TheirLower" not in body
        assert session_url(theirs.session) not in body

    def test_excludes_inactive_link(self, client):
        s = seed(
            link_status=CoachAthlete.Status.PENDING_COACH_INVITE,
            session_name="PendingLower",
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "PendingLower" not in body

    def test_excludes_archived_plan(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED, session_name="ArchivedLower")
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "ArchivedLower" not in body

    def test_session_status_reflects_log(self, client):
        s = seed()
        SessionLogFactory(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "Logged" in body

    def test_session_status_pending_without_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "To do" in body

    def test_another_athletes_log_does_not_mark_done(self, client):
        """A log belonging to a *different* athlete must not flip my session done."""
        s = seed()
        other = UserFactory()
        SessionLogFactory(
            session=s.session, athlete=other, status=SessionLog.Status.DONE
        )
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert "To do" in body


# -- athlete session detail ------------------------------------------------


class TestAthleteSession:
    def test_requires_login(self, client):
        s = seed()
        resp = client.get(session_url(s.session))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_shows_prescribed_exercises(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.get(session_url(s.session))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Box Squat" in body
        assert "Lower" in body
        # The prescribed target is visible.
        assert "6" in body

    def test_404_for_other_athlete(self, client):
        s = seed()
        intruder = seed().athlete
        client.force_login(intruder)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_when_undelivered(self, client):
        s = seed(delivered=False)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_inactive_link(self, client):
        s = seed(link_status=CoachAthlete.Status.ENDED)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_archived_plan(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED)
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_404_unknown_session(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            client.get(
                reverse("meso:athlete_session", kwargs={"pk": 999999})
            ).status_code
            == 404
        )


# -- roster routing for pure athletes --------------------------------------


class TestRosterRedirect:
    def test_pure_athlete_redirected_to_home(self, client):
        s = seed()
        # ``s.athlete`` has an active coach link but no CoachProfile.
        client.force_login(s.athlete)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 302
        assert resp.url == HOME

    def test_coach_stays_on_roster(self, client):
        s = seed()
        CoachProfileFactory(user=s.coach)
        client.force_login(s.coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200

    def test_coach_who_is_also_athlete_stays_on_roster(self, client):
        s = seed()
        # The coach also trains under someone else *and* coaches — stays on roster.
        CoachProfileFactory(user=s.coach)
        seed(athlete=s.coach)
        client.force_login(s.coach)
        resp = client.get(reverse("meso:roster"))
        assert resp.status_code == 200
