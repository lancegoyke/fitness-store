"""Athlete slice Phase 2 — session logging (the write path).

The athlete's delivered session screen becomes the interactive logger:
``POST /meso/api/me/session/<id>/log/`` upserts the athlete's own ``SessionLog``
(status, date, notes), flips the session done, and stamps the date. Sets are no
longer posted here (#578 stage 4): they are typed lines saved by
``athlete_cell_write``, so these tests log sets through that path. These are the first *real* logged rows — the ones
``serialize_recent_logs`` grounds the agent on (every log before this slice was
fabricated in tests).

The tests pin the same discipline as the read surface (see
``docs/archive/meso/athlete-plan.md``): the endpoint is athlete-scoped (only the
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
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.serializers import serialize_recent_logs
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
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
        delivered_at=timezone.now() if delivered else None,
    )
    session = day(week, day_number=1, name="Lower", bias="Quad")
    squat = presc(
        session,
        name="Box Squat",
        order=0,
        sets="3",
        reps="6",
        load="70",
        rpe="7",
    )
    rdl = presc(session, name="RDL", order=1, sets="3", reps="8", load="80", rpe="8")
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


def type_line(client, s, exercise, line, text):
    """Log a set the only way left: type it on a sub-line (``athlete_cell_write``)."""
    resp = client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": s.session.pk}),
        data=json.dumps({"exercise_id": exercise.pk, "line": line, "text": text}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp


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

    def test_undelivered_session_is_loggable(self, client):
        """Edits are live (2d): delivery never gates logging."""
        s = seed(delivered=False)
        client.force_login(s.athlete)
        assert post(client, s.session, {"sets": []}).status_code == 200
        assert SessionLog.objects.count() == 1

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
    def test_posting_creates_done_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = post(client, s.session, {})
        assert resp.status_code == 200
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.DONE
        # Finishing stamps today's date when none is given.
        assert log.date == timezone.localdate()
        assert log.sets.count() == 0

    def test_skipping_a_logged_row_preserves_its_logged_sets(self, client):
        # A coach marks a row skipped AFTER the athlete logged it. The row drops
        # out of the trainable cells, and a later "Finish session" must NOT wipe
        # the skipped row's logged history.
        s = seed()
        client.force_login(s.athlete)
        type_line(client, s, s.squat, 1, "70 x 6, RPE 7")
        type_line(client, s, s.rdl, 1, "80 x 8, RPE 8")
        assert LoggedSet.objects.filter(prescription=s.squat).count() == 1

        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])

        resp = post(client, s.session, {"status": "done"})
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(prescription=s.squat).count() == 1
        assert LoggedSet.objects.filter(prescription=s.rdl, load="80").count() == 1

    def test_response_echoes_saved_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        type_line(client, s, s.squat, 1, "100 x 5, RPE 9")
        data = post(client, s.session, {"status": "done", "notes": "good"}).json()
        assert data["ok"] is True
        assert data["log"]["status"] == "done"
        assert data["log"]["notes"] == "good"
        assert data["log"]["date"] == timezone.localdate().isoformat()
        assert "sets" not in data["log"]
        assert "new_records" not in data
        assert data["progress"]["logged"] == 1

    def test_relog_updates_same_log(self, client):
        """Finishing the same session twice updates the one log -- no duplicate rows."""
        s = seed()
        client.force_login(s.athlete)
        type_line(client, s, s.squat, 1, "70 x 6, RPE 7")
        post(client, s.session, {"status": "pending"})
        post(client, s.session, {"status": "done"})
        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 1
        )
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.DONE
        # The typed set is neither replaced nor duplicated by the log posts.
        assert log.sets.count() == 1

    def test_accepts_explicit_status_pending(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"status": "pending"})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.PENDING

    def test_accepts_explicit_date(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"date": "2026-06-20"})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.date == datetime.date(2026, 6, 20)

    def test_relog_without_date_keeps_original_date(self, client):
        """A later finish (no date sent) must not move the workout to today."""
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"date": "2026-06-20"})
        # ...then a later post that omits the date entirely.
        post(client, s.session, {"status": "done"})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.date == datetime.date(2026, 6, 20)  # not today

    def test_saves_notes(self, client):
        s = seed()
        client.force_login(s.athlete)
        post(client, s.session, {"notes": "Knee felt great."})
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.notes == "Knee felt great."

    def test_an_empty_body_marks_done(self, client):
        """An athlete can mark a session done without logging every set."""
        s = seed()
        client.force_login(s.athlete)
        resp = post(client, s.session, {})
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


# -- the logger surfaces the prescribed target -----------------------------


class TestLogPage:
    def test_shows_prescribed_load_and_rpe(self, client):
        """The athlete sees the coach's prescribed load/RPE before logging."""
        s = seed()  # Box Squat is prescribed 3 × 6 · load 70 · RPE 7
        client.force_login(s.athlete)
        body = client.get(session_url(s.session)).content.decode()
        assert "70" in body
        assert "RPE 7" in body

    def test_names_the_athlete_whose_offline_queue_it_may_flush(self, client):
        """#527: the offline queue outlasts a logout, so the page says whose it is.

        Without it, the next athlete on the device would replay the last one's
        queued writes under their own login.
        """
        s = seed()
        client.force_login(s.athlete)
        resp = client.get(session_url(s.session))
        assert resp.context["log_data"]["owner"] == str(s.athlete.pk)


# -- closes the loop: logged rows survive reload + reach the agent ---------


class TestLogFeedsBack:
    def test_logged_session_survives_reload(self, client):
        s = seed()
        client.force_login(s.athlete)
        type_line(client, s, s.squat, 1, "92.5 x 6, RPE 8")
        post(client, s.session, {"status": "done"})
        # Reloading the session screen reflects what was logged.
        body = client.get(session_url(s.session)).content.decode()
        assert "92.5" in body
        # And the session now reads as logged.
        assert "Logged" in body

    def test_logged_session_grounds_the_agent(self, client):
        s = seed()
        client.force_login(s.athlete)
        type_line(client, s, s.squat, 1, "105 x 5, RPE 9")
        post(client, s.session, {"status": "done"})
        # The very rows the agent's grounding reads (serialize_recent_logs).
        recent = serialize_recent_logs(s.plan)
        assert len(recent) == 1
        assert recent[0]["status"] == "done"
        assert recent[0]["sets"][0]["exercise"] == "Box Squat"
        assert recent[0]["sets"][0]["load"] == "105"
        # And through the agent's context builder.
        context = service.build_context(s.plan, s.plan.mesocycles.first())
        assert context["recent_logs"][0]["sets"][0]["load"] == "105"
