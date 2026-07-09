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
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc as make_presc
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
    session = day(week, day_number=1, name=session_name, bias="Quad")
    presc = make_presc(
        session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
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


def seed_block(
    *,
    coach=None,
    athlete=None,
    skip_first=False,
    swap_second="",
    third_delivered=False,
):
    """A three-week block of ONE day × ONE exercise row (P3 athlete multi-week).

    All three weeks share one ``SessionSlot`` (the day) and one ``ExerciseSlot``
    (the row), with a per-week ``Prescription`` cell carrying a distinct load so
    each week's column is identifiable. Weeks 1 & 2 are delivered (the whole
    block delivers at once); week 2 is the athlete's ``is_current`` week; week 3
    is left undelivered unless ``third_delivered`` (a week the coach is still
    building — invisible to the athlete).
    """
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
    w1 = WeekFactory(mesocycle=meso, index=1, is_current=False, delivered_at=now)
    w2 = WeekFactory(mesocycle=meso, index=2, is_current=True, delivered_at=now)
    w3 = WeekFactory(
        mesocycle=meso,
        index=3,
        is_current=False,
        delivered_at=now if third_delivered else None,
    )
    s1 = day(w1, session_slot=slot)
    s2 = day(w2, session_slot=slot)
    s3 = day(w3, session_slot=slot)
    c1 = make_presc(
        exercise_slot=ex,
        week=w1,
        sets="4",
        reps="8",
        load="71",
        rpe="7",
        skipped=skip_first,
    )
    c2 = make_presc(
        exercise_slot=ex,
        week=w2,
        sets="4",
        reps="8",
        load="101",
        rpe="8",
        swap_name=swap_second,
    )
    c3 = make_presc(exercise_slot=ex, week=w3, sets="4", reps="8", load="131", rpe="8")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        plan=plan,
        meso=meso,
        slot=slot,
        ex=ex,
        w1=w1,
        w2=w2,
        w3=w3,
        s1=s1,
        s2=s2,
        s3=s3,
        c1=c1,
        c2=c2,
        c3=c3,
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

    # -- multi-week block (P3) --------------------------------------------

    def test_shows_every_delivered_week_of_the_block(self, client):
        """The card renders the WHOLE delivered block, not just the latest week.

        The read-only multi-week table has a column per delivered week, each
        carrying that week's own prescription summary.
        """
        b = seed_block()
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        # A column per delivered week (labels + their distinct per-week loads).
        assert "Wk 1" in body
        assert "Wk 2" in body
        assert "71 kg" in body  # week-1 cell summary
        assert "101 kg" in body  # week-2 cell summary

    def test_undelivered_week_is_not_a_column(self, client):
        """A week the coach hasn't delivered yet never becomes an athlete column."""
        b = seed_block()  # week 3 undelivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Wk 3" not in body
        assert "131 kg" not in body  # its cell is never rendered

    def test_home_focuses_the_current_delivered_week(self, client):
        """The home opens to ``is_current``: only its sessions are tappable rows.

        Earlier delivered weeks live in the read-only table — their sessions are
        cells, not links to the logger.
        """
        b = seed_block()  # week 2 is_current + delivered
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(b.s2) in body  # current week's session logs
        assert session_url(b.s1) not in body  # earlier week is read-only

    def test_skipped_cell_renders_em_dash(self, client):
        """A one-week skip shows an em-dash in its cell, not a prescription."""
        b = seed_block(skip_first=True)
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "—" in body  # em-dash present
        assert "71 kg" not in body  # the skipped week shows no numbers
        assert "101 kg" in body  # the other delivered week still does

    def test_swapped_cell_shows_the_swapped_name(self, client):
        """A one-week swap surfaces the swapped exercise's name in its cell."""
        b = seed_block(swap_second="Front Squat")
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Front Squat" in body  # the swap
        assert "Box Squat" in body  # the underlying slot row still labels the row

    def test_block_table_comment_does_not_leak_onto_the_page(self, client):
        """The block table's template-author note stays out of the rendered HTML.

        Django's ``{# #}`` comment is single-line only, so a multi-line one would
        render verbatim onto the athlete's screen; the note must use
        ``{% comment %}`` (regression guard).
        """
        b = seed_block()
        client.force_login(b.athlete)
        body = client.get(HOME).content.decode()
        assert "Read-only multi-week" not in body
        assert "server-rendered" not in body
        assert "#}" not in body


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


# -- soft delete (designer framework Phase 0) --------------------------------


class TestSoftDeletedRowsHidden:
    """A coach's soft-delete must reach the athlete surface, not just the designer.

    Deleting an already-delivered day/exercise stamps ``deleted_at``; the
    athlete must stop seeing it — home no longer lists the day, its logger
    404s, and log/1RM writes against a deleted row are rejected — while every
    log already recorded survives (that survival is pinned by the designer
    delete tests; here we pin the *hiding*).
    """

    def _log_post(self, client, session, payload):
        import json

        return client.post(
            reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_home_hides_soft_deleted_session(self, client):
        s = seed(session_name="Lower")
        s.session.deleted_at = timezone.now()
        s.session.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        body = client.get(HOME).content.decode()
        assert session_url(s.session) not in body
        assert "Lower" not in body

    def test_session_page_404s_when_session_soft_deleted(self, client):
        s = seed()
        s.session.deleted_at = timezone.now()
        s.session.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_session_page_404s_when_week_soft_deleted(self, client):
        s = seed()
        s.week.deleted_at = timezone.now()
        s.week.save(update_fields=["deleted_at"])
        client.force_login(s.athlete)
        assert client.get(session_url(s.session)).status_code == 404

    def test_logger_hides_soft_deleted_prescription(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        body = client.get(session_url(s.session)).content.decode()
        assert "Ghost Curl" not in body
        assert "Box Squat" in body

    def test_logging_a_soft_deleted_prescription_is_rejected(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        resp = self._log_post(
            client,
            s.session,
            {"sets": [{"prescription": ghost.pk, "reps": "8", "load": "60"}]},
        )
        assert resp.status_code == 400
        assert SessionLog.objects.count() == 0

    def test_one_rm_on_a_soft_deleted_prescription_is_rejected(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        ghost.exercise_slot.soft_delete()
        client.force_login(s.athlete)
        import json

        resp = client.post(
            reverse("meso:athlete_set_one_rm", kwargs={"pk": s.session.pk}),
            data=json.dumps({"prescription": ghost.pk, "value": "140"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_resave_preserves_a_hidden_prescriptions_logged_sets(self, client):
        s = seed()
        ghost = make_presc(s.session, name="Ghost Curl")
        client.force_login(s.athlete)
        resp = self._log_post(
            client,
            s.session,
            {
                "sets": [
                    {"prescription": s.presc.pk, "reps": "6", "load": "70"},
                    {"prescription": ghost.pk, "reps": "8", "load": "40"},
                ]
            },
        )
        assert resp.status_code == 200

        # The coach removes Ghost Curl; the logger no longer shows it, so the
        # athlete's next save posts only the surviving lift. The hidden row's
        # already-logged set must NOT be wiped by the save's replace step.
        ghost.exercise_slot.soft_delete()
        resp = self._log_post(
            client,
            s.session,
            {"sets": [{"prescription": s.presc.pk, "reps": "5", "load": "75"}]},
        )
        assert resp.status_code == 200

        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        ghost_sets = log.sets.filter(prescription=ghost)
        assert ghost_sets.count() == 1
        assert ghost_sets.get().reps == "8"
        live_sets = log.sets.filter(prescription=s.presc)
        assert live_sets.count() == 1
        assert live_sets.get().reps == "5"
