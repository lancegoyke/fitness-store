"""Athlete slice Phase 2 — session logging (the write path).

The athlete's delivered session screen becomes the interactive logger:
``POST /meso/api/me/session/<id>/log/`` upserts the athlete's own ``SessionLog``
and its ``LoggedSet`` rows (reps/load/rpe per set), flips the session done, and
stamps the date. These are the first *real* logged rows — the ones
``serialize_recent_logs`` grounds the agent on (every log before this slice was
fabricated in tests).

The tests pin the same discipline as the read surface (see
``docs/meso/athlete-plan.md``): the endpoint is athlete-scoped (only the
logged-in athlete, only a **delivered** session they own), every out-of-scope
target is a flat 404, bad input is a 400 that writes nothing, and the write is
idempotent (re-logging updates the same ``SessionLog`` rather than piling up
rows). A final pair of tests confirms a logged session survives reload and
reaches the agent's grounding.
"""

import datetime
import json
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.agent import service
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.serializers import serialize_recent_logs
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed(
    *,
    coach=None,
    athlete=None,
    delivered=True,
    link_status=CoachAthlete.Status.ACTIVE,
    plan_status=Plan.Status.ACTIVE,
):
    """A minimal plan → (optionally delivered) week → session → two prescriptions."""
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
    session = SessionFactory(week=week, day_number=1, name="Lower", bias="Quad")
    squat = ExercisePrescriptionFactory(
        session=session,
        name="Box Squat",
        order=0,
        sets="3",
        reps="6",
        load="70",
        rpe="7",
    )
    rdl = ExercisePrescriptionFactory(
        session=session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8"
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


def log_url(session):
    return reverse("meso:athlete_log_session", kwargs={"pk": session.pk})


def session_url(session):
    return reverse("meso:athlete_session", kwargs={"pk": session.pk})


def post(client, session, payload):
    return client.post(
        log_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


# -- access control --------------------------------------------------------


class TestLogAccessControl:
    def test_requires_login(self, client):
        s = seed()
        resp = post(client, s.session, {"sets": []})
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url
        assert SessionLog.objects.count() == 0

    def test_get_not_allowed(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert client.get(log_url(s.session)).status_code == 405

    def test_404_for_other_athlete(self, client):
        s = seed()
        intruder = seed().athlete
        client.force_login(intruder)
        resp = post(client, s.session, {"sets": []})
        assert resp.status_code == 404
        assert SessionLog.objects.count() == 0

    def test_404_when_undelivered(self, client):
        s = seed(delivered=False)
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": []}).status_code == 404
        assert SessionLog.objects.count() == 0

    def test_404_inactive_link(self, client):
        s = seed(link_status=CoachAthlete.Status.ENDED)
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": []}).status_code == 404

    def test_404_archived_plan(self, client):
        s = seed(plan_status=Plan.Status.ARCHIVED)
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": []}).status_code == 404

    def test_404_unknown_session(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": 999999}),
            data=json.dumps({"sets": []}),
            content_type="application/json",
        )
        assert resp.status_code == 404


# -- write semantics -------------------------------------------------------


class TestLogWrite:
    def test_logs_sets_creates_done_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "72.5",
                        "rpe": "8",
                    },
                    {
                        "prescription": s.squat.pk,
                        "set_number": 2,
                        "reps": "6",
                        "load": "72.5",
                        "rpe": "8.5",
                    },
                ]
            },
        )
        assert resp.status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.DONE
        # Logging stamps today's date when none is given.
        assert log.date == timezone.localdate()
        sets = list(log.sets.order_by("set_number"))
        assert len(sets) == 2
        assert sets[0].prescription_id == s.squat.pk
        assert sets[0].reps == "6"
        assert sets[0].load == "72.5"
        assert sets[1].rpe == "8.5"

    def test_response_echoes_saved_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "100",
                        "rpe": "9",
                    }
                ]
            },
        )
        data = resp.json()
        assert data["ok"] is True
        assert data["log"]["status"] == "done"
        assert data["log"]["sets"][0]["load"] == "100"
        assert data["log"]["sets"][0]["prescription"] == s.squat.pk

    def test_relog_updates_same_log(self, client):
        """Re-logging the same session updates the one log — no duplicate rows."""
        s = seed()
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "70",
                        "rpe": "7",
                    }
                ]
            },
        )
        post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "80",
                        "rpe": "9",
                    }
                ]
            },
        )
        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 1
        )
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        sets = list(log.sets.all())
        # The old set row is replaced, not appended.
        assert len(sets) == 1
        assert sets[0].load == "80"

    def test_accepts_explicit_status_pending(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"status": "pending", "sets": []})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.PENDING

    def test_accepts_explicit_date(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"date": "2026-06-20", "sets": []})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.date == datetime.date(2026, 6, 20)

    def test_saves_notes(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"notes": "Knee felt great.", "sets": []})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.notes == "Knee felt great."

    def test_empty_sets_marks_done(self, client):
        """An athlete can mark a session done without logging every set."""
        s = seed()
        client.force_login(s.athlete)
        resp = post(client, s.session, {"sets": []})
        assert resp.status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.DONE
        assert log.sets.count() == 0


# -- validation ------------------------------------------------------------


class TestLogValidation:
    def test_malformed_json(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            log_url(s.session), data="not json", content_type="application/json"
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0

    def test_non_dict_body(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = client.post(
            log_url(s.session), data="[1,2,3]", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_sets_must_be_list(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": {"nope": 1}}).status_code == 400

    def test_set_must_be_object(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": ["nope"]}).status_code == 400
        assert SessionLog.objects.count() == 0

    def test_bad_status(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert post(client, s.session, {"status": "wat", "sets": []}).status_code == 400

    def test_bad_date(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            post(client, s.session, {"date": "not-a-date", "sets": []}).status_code
            == 400
        )

    def test_foreign_prescription_rejected(self, client):
        """A set pointing at a prescription from another session is a 400 — no write."""
        s = seed()
        other = seed()  # an unrelated plan/session/prescription
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"sets": [{"prescription": other.squat.pk, "set_number": 1, "reps": "5"}]},
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0
        assert LoggedSet.objects.count() == 0

    def test_missing_prescription_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            post(
                client, s.session, {"sets": [{"set_number": 1, "reps": "5"}]}
            ).status_code
            == 400
        )

    def test_non_int_prescription_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(client, s.session, {"sets": [{"prescription": "x", "reps": "5"}]})
        assert resp.status_code == 400

    def test_non_string_field_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {
                "sets": [{"prescription": s.squat.pk, "reps": 5}]
            },  # reps must be a string
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0

    def test_overlong_field_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {
                "sets": [{"prescription": s.squat.pk, "load": "x" * 40}]
            },  # load max_length 32
        )
        assert resp.status_code == 400

    def test_bad_set_number_rejected(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"sets": [{"prescription": s.squat.pk, "set_number": 0, "reps": "5"}]},
        )
        assert resp.status_code == 400

    def test_excessive_set_number_rejected(self, client):
        """A wild set_number can't be stored — it would balloon the next render."""
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"sets": [{"prescription": s.squat.pk, "set_number": 10_000, "reps": "5"}]},
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0


# -- ownership isolation ---------------------------------------------------


class TestLogOwnership:
    def test_logging_does_not_touch_another_athletes_log(self, client):
        s = seed()
        other = UserFactory()
        # A pre-existing log on the *same* session belonging to a different athlete.
        SessionLog.objects.create(
            session=s.session, athlete=other, status=SessionLog.Status.PENDING
        )
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "100",
                    }
                ]
            },
        )
        # The other athlete's log is untouched; mine is separate and done.
        assert SessionLog.objects.filter(session=s.session, athlete=other).count() == 1
        assert SessionLog.objects.get(session=s.session, athlete=other).status == (
            SessionLog.Status.PENDING
        )
        mine = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert mine.status == SessionLog.Status.DONE


# -- closes the loop: logged rows survive reload + reach the agent ---------


class TestLogFeedsBack:
    def test_logged_session_survives_reload(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "6",
                        "load": "92.5",
                        "rpe": "8",
                    }
                ]
            },
        )
        # Reloading the session screen reflects what was logged.
        body = client.get(session_url(s.session)).content.decode()
        assert "92.5" in body
        # And the session now reads as logged.
        assert "Logged" in body

    def test_logged_session_grounds_the_agent(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.squat.pk,
                        "set_number": 1,
                        "reps": "5",
                        "load": "105",
                        "rpe": "9",
                    }
                ]
            },
        )
        # The very rows the agent's grounding reads (serialize_recent_logs).
        recent = serialize_recent_logs(s.plan)
        assert len(recent) == 1
        assert recent[0]["status"] == "done"
        assert recent[0]["sets"][0]["exercise"] == "Box Squat"
        assert recent[0]["sets"][0]["load"] == "105"
        # And through the agent's context builder.
        context = service.build_context(s.plan)
        assert context["recent_logs"][0]["sets"][0]["load"] == "105"
