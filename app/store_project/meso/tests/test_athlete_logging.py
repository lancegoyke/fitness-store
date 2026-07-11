"""Athlete slice Phase 2 — session logging (the write path).

The athlete's delivered session screen becomes the interactive logger:
``POST /meso/api/me/session/<id>/log/`` upserts the athlete's own ``SessionLog``
and its ``LoggedSet`` rows (reps/load/rpe per set), flips the session done, and
stamps the date. These are the first *real* logged rows — the ones
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
from store_project.meso.factories import MesoGroupFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.models import Week
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
        is_current=True,
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

    def test_skipping_a_logged_row_preserves_its_logged_sets(self, client):
        # A coach marks a row skipped AFTER the athlete logged it. The row drops
        # from the logger (trainable_cells), so the next save posts only the
        # remaining rows — and must NOT wipe the skipped row's logged history.
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
                    },
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "8",
                    },
                ]
            },
        )
        assert LoggedSet.objects.filter(prescription=s.squat).count() == 1

        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])

        # The logger now only renders/posts the RDL; the re-save must leave the
        # squat's logged history intact.
        resp = post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "82",
                        "rpe": "8",
                    },
                ]
            },
        )
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(prescription=s.squat).count() == 1
        assert LoggedSet.objects.filter(prescription=s.rdl, load="82").count() == 1

    def test_skipped_row_is_not_a_valid_log_target(self, client):
        # A skipped cell isn't rendered by the logger, so posting a set against it
        # is a 400 (the same guard as a foreign prescription).
        s = seed()
        s.squat.skipped = True
        s.squat.save(update_fields=["skipped"])
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {"sets": [{"prescription": s.squat.pk, "reps": "6", "load": "70"}]},
        )
        assert resp.status_code == 400

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

    def test_relog_without_date_keeps_original_date(self, client):
        """Editing a set later (no date sent) must not move the workout to today."""
        s = seed()
        client.force_login(s.athlete)
        # Logged as having trained on the 20th...
        post(
            client,
            s.session,
            {
                "date": "2026-06-20",
                "sets": [{"prescription": s.squat.pk, "set_number": 1, "reps": "6"}],
            },
        )
        # ...then a later edit that omits the date entirely.
        post(
            client,
            s.session,
            {"sets": [{"prescription": s.squat.pk, "set_number": 1, "reps": "5"}]},
        )
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.date == datetime.date(2026, 6, 20)  # not today

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

    def test_duplicate_set_key_rejected(self, client):
        """Two sets with the same (prescription, set_number) are ambiguous — 400."""
        s = seed()
        client.force_login(s.athlete)
        resp = post(
            client,
            s.session,
            {
                "sets": [
                    {"prescription": s.squat.pk, "set_number": 1, "reps": "6"},
                    {"prescription": s.squat.pk, "set_number": 1, "reps": "5"},
                ]
            },
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0
        assert LoggedSet.objects.count() == 0


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


# -- issue #456: logging auto-advances the plan's is_current pointer -------


class TestLogAdvancesCurrentWeek:
    """A log write moves ``Week.is_current`` forward to the week it belongs to.

    Locked rule: logging (pending OR done) a session in a week LATER than the
    plan's current week advances the pointer there — forward-only, tuple
    compared on ``(mesocycle.order, week.index)`` since ``is_current`` is
    plan-wide (see ``Week.advance_current_week``). The coach's manual
    ``week_set_current`` override is untouched by this behavior.
    """

    def _two_week_plan(
        self,
        *,
        coach=None,
        athlete=None,
        current_index=1,
        other_index=2,
        delivered=True,
    ):
        """One mesocycle, two weeks sharing a day/row, each independently loggable."""
        coach = coach or UserFactory()
        athlete = athlete or UserFactory()
        rel = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        plan = PlanFactory(
            relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
        )
        meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
        now = timezone.now()
        slot = SessionSlot.objects.create(
            mesocycle=meso, day_number=1, name="Lower", bias="Quad", order=0
        )
        ex = ExerciseSlot.objects.create(session_slot=slot, name="Box Squat", order=0)
        week_a = WeekFactory(
            mesocycle=meso,
            index=current_index,
            is_current=True,
            delivered_at=now if delivered else None,
        )
        week_b = WeekFactory(
            mesocycle=meso,
            index=other_index,
            is_current=False,
            delivered_at=now if delivered else None,
        )
        session_a = day(week_a, session_slot=slot)
        session_b = day(week_b, session_slot=slot)
        presc(exercise_slot=ex, week=week_a, sets="3", reps="6", load="70", rpe="7")
        presc(exercise_slot=ex, week=week_b, sets="3", reps="6", load="75", rpe="7")
        return SimpleNamespace(
            coach=coach,
            athlete=athlete,
            rel=rel,
            plan=plan,
            meso=meso,
            week_a=week_a,
            week_b=week_b,
            session_a=session_a,
            session_b=session_b,
        )

    def test_logging_a_later_week_done_advances_and_clears_the_old_one(self, client):
        s = self._two_week_plan()  # week_a (idx 1) current, week_b (idx 2) later
        client.force_login(s.athlete)
        resp = post(client, s.session_b, {"sets": []})
        assert resp.status_code == 200
        s.week_a.refresh_from_db()
        s.week_b.refresh_from_db()
        assert s.week_b.is_current is True
        assert s.week_a.is_current is False
        assert Week.objects.filter(mesocycle__plan=s.plan, is_current=True).count() == 1

    def test_logging_a_later_week_pending_also_advances(self, client):
        s = self._two_week_plan()
        client.force_login(s.athlete)
        resp = post(client, s.session_b, {"status": "pending", "sets": []})
        assert resp.status_code == 200
        s.week_a.refresh_from_db()
        s.week_b.refresh_from_db()
        assert s.week_b.is_current is True
        assert s.week_a.is_current is False

    def test_logging_the_current_week_is_a_noop(self, client):
        s = self._two_week_plan()
        client.force_login(s.athlete)
        resp = post(client, s.session_a, {"sets": []})
        assert resp.status_code == 200
        s.week_a.refresh_from_db()
        s.week_b.refresh_from_db()
        assert s.week_a.is_current is True
        assert s.week_b.is_current is False

    def test_logging_an_earlier_week_is_a_noop(self, client):
        # week_a (idx 2) is current; week_b (idx 1) is earlier.
        s = self._two_week_plan(current_index=2, other_index=1)
        client.force_login(s.athlete)
        resp = post(client, s.session_b, {"sets": []})
        assert resp.status_code == 200
        s.week_a.refresh_from_db()
        s.week_b.refresh_from_db()
        assert s.week_a.is_current is True
        assert s.week_b.is_current is False

    def test_cross_mesocycle_advance_uses_tuple_order_not_bare_index(self, client):
        # week1 lives in mesocycle order=0 at index 2 (current); week2 lives in a
        # LATER mesocycle (order=1) at index 1 — a lower bare index, but later in
        # plan-wide (mesocycle.order, index) tuple order.
        coach = UserFactory()
        athlete = UserFactory()
        rel = CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        meso1 = MesocycleFactory(plan=plan, order=0)
        meso2 = MesocycleFactory(plan=plan, order=1)
        now = timezone.now()
        week1 = WeekFactory(mesocycle=meso1, index=2, is_current=True, delivered_at=now)
        week2 = WeekFactory(
            mesocycle=meso2, index=1, is_current=False, delivered_at=now
        )
        session2 = day(week2, day_number=1, name="Lower")
        presc(session2, name="Squat")
        client.force_login(athlete)
        resp = post(client, session2, {"sets": []})
        assert resp.status_code == 200
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week2.is_current is True
        assert week1.is_current is False

    def test_self_heals_when_plan_has_no_current_week(self, client):
        s = self._two_week_plan()
        s.week_a.is_current = False
        s.week_a.save(update_fields=["is_current"])
        client.force_login(s.athlete)
        resp = post(client, s.session_b, {"sets": []})
        assert resp.status_code == 200
        s.week_a.refresh_from_db()
        s.week_b.refresh_from_db()
        assert s.week_b.is_current is True
        assert s.week_a.is_current is False

    def test_advances_a_group_materialized_member_plan_not_the_shared_pointer(
        self, client
    ):
        coach = UserFactory()
        group = MesoGroupFactory(coach=coach)
        athlete = UserFactory()
        CoachAthleteFactory(
            coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
        )
        group.add_athlete(athlete)
        shared_plan = group.create_shared_plan()
        shared_meso = shared_plan.mesocycles.get()
        shared_meso.append_week()  # a second live week; week 1 stays current

        _, delivered = group.deliver_block(shared_plan)
        member_plan, member_weeks = delivered[0]
        member_week1, member_week2 = member_weeks  # index order

        client.force_login(athlete)
        session2 = member_week2.sessions.first()
        resp = post(client, session2, {"sets": []})
        assert resp.status_code == 200

        member_week1.refresh_from_db()
        member_week2.refresh_from_db()
        assert member_week2.is_current is True
        assert member_week1.is_current is False

        # The group's own shared pointer is untouched by a member's log.
        shared_week1 = shared_meso.weeks.get(index=1)
        assert shared_week1.is_current is True

    def test_advance_does_not_append_to_the_undo_stack(self, client):
        s = self._two_week_plan()
        client.force_login(s.athlete)
        post(client, s.session_b, {"sets": []})
        assert PlanAction.objects.filter(plan=s.plan).count() == 0

    # -- issue #456 Finding 2: stale `self` under a concurrent pointer move --

    def test_stale_self_survives_a_concurrent_earlier_pointer_move(self):
        """The compare/save must use a lock-fresh row, not the pre-lock `self`.

        Race: an athlete's Week instance is loaded (``is_current`` True) before
        a concurrent write; the coach's "Make current" then moves the pointer
        to an EARLIER week and commits, clearing our week's flag in the DB out
        from under the in-memory instance — without touching the in-memory
        ``self.is_current``, which is still stale ``True``. Calling that STALE
        instance's ``advance_current_week`` must still land on exactly one
        current week — itself — not zero. (Pre-fix: ``live_weeks`` decided to
        proceed off a fresh read, but the final ``if not self.is_current``
        check and the eventual save used the stale ``self`` — its stale
        ``True`` short-circuited the save entirely, after the bulk-clear had
        already wiped the earlier week's flag, leaving the plan with zero
        current weeks.)
        """
        s = self._two_week_plan(current_index=2, other_index=1)
        # week_a (index 2, "w2") starts current; week_b (index 1, "w1") is earlier.
        stale = Week.objects.get(pk=s.week_a.pk)
        assert stale.is_current is True

        # A concurrent coach "Make current" moves the pointer back to the
        # EARLIER week and commits — mimicking a write that lands between our
        # instance loading and its (later) ``advance_current_week`` call.
        Week.objects.filter(pk=s.week_a.pk).update(is_current=False)
        Week.objects.filter(pk=s.week_b.pk).update(is_current=True)

        assert stale.advance_current_week() is True

        live_current = Week.objects.filter(
            mesocycle__plan=s.plan, is_current=True, deleted_at__isnull=True
        )
        assert live_current.count() == 1
        assert live_current.get().pk == stale.pk
        assert stale.is_current is True  # the caller's instance stays coherent

    def test_stale_self_concurrently_soft_deleted_is_a_noop(self):
        s = self._two_week_plan()  # week_a (idx 1) current; week_b (idx 2) later
        stale = Week.objects.get(pk=s.week_b.pk)

        # The week the caller loaded is concurrently soft-deleted before the
        # lock is acquired (e.g. the coach deleted its day/row build mid-race).
        Week.objects.filter(pk=s.week_b.pk).update(deleted_at=timezone.now())

        assert stale.advance_current_week() is False

        s.week_a.refresh_from_db()
        assert s.week_a.is_current is True  # the pointer is untouched
        assert (
            Week.objects.filter(
                mesocycle__plan=s.plan, is_current=True, deleted_at__isnull=True
            ).count()
            == 1
        )
