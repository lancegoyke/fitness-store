"""Parse-at-commit — the write hook (5a stage 3, docs/meso/parse-at-commit-plan.md §5).

``athlete_cell_write`` (the freeform sub-line save, Phase 4a) now also parses
the just-saved text with ``parse_performed`` and upserts a silent, derivative
``LoggedSet`` scoped to ``(session_log, source_line)`` — delete-then-recreate,
idempotent, never blocking entry. This mirrors the structured logger
(``athlete_log_session``) closely enough that the two write paths must never
clobber each other: the structured logger's own delete is scoped to
``source_line__isnull=True`` so a "Save progress" never wipes a freeform-parsed
set (and vice versa, since the cell-write upsert only ever touches its own
``source_line``).
"""

import json
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
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
    """A minimal plan → (optionally delivered) week → session → two line-0 cells."""
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
        session, name="Box Squat", order=0, sets="3", reps="6", load="70", rpe="7"
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


def cell_url(session):
    return reverse("meso:athlete_cell_write", kwargs={"pk": session.pk})


def cell_post(client, session, payload):
    return client.post(
        cell_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


def write_cell(client, session, exercise, line, text):
    return cell_post(
        client, session, {"exercise_id": exercise.pk, "line": line, "text": text}
    )


def log_url(session):
    return reverse("meso:athlete_log_session", kwargs={"pk": session.pk})


def log_post(client, session, payload):
    return client.post(
        log_url(session),
        data=json.dumps(payload),
        content_type="application/json",
    )


def the_log(session, athlete):
    return SessionLog.objects.get(session=session, athlete=athlete)


def sub_cell(exercise, line=1):
    return Prescription.objects.get(
        exercise_slot=exercise.exercise_slot, week=exercise.week, line=line
    )


# -- upsert / idempotency ----------------------------------------------------


class TestParsedSetUpsert:
    def test_set_text_creates_a_logged_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        log = the_log(s.session, s.athlete)
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(session_log=log)
        assert row.source_line_id == cell.pk
        assert row.prescription_id == s.squat.pk
        assert row.set_number == 1
        assert row.load == "225"
        assert row.reps == "5"

    def test_reblur_replaces_not_appends(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 1, "230 x 3")

        log = the_log(s.session, s.athlete)
        cell = sub_cell(s.squat, 1)
        rows = list(LoggedSet.objects.filter(session_log=log, source_line=cell))
        assert len(rows) == 1
        assert rows[0].load == "230"
        assert rows[0].reps == "3"

    def test_blank_text_deletes_the_parsed_set(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        resp = write_cell(client, s.session, s.squat, 1, "")
        assert resp.status_code == 200
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_unparseable_text_writes_no_set_but_keeps_text_and_returns_200(
        self, client
    ):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "felt tight")
        assert resp.status_code == 200

        cell = sub_cell(s.squat, 1)
        assert cell.text == "felt tight"
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    @pytest.mark.parametrize("text", ["skip", "-", "DB pullover", "20-60m", "225 x"])
    def test_non_set_classifications_write_no_set(self, client, text):
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, text)
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        assert cell.text == text
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_two_sub_lines_make_two_rows_with_distinct_source_line(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        write_cell(client, s.session, s.squat, 2, "230 x 3")

        log = the_log(s.session, s.athlete)
        cell1 = sub_cell(s.squat, 1)
        cell2 = sub_cell(s.squat, 2)
        rows = list(LoggedSet.objects.filter(session_log=log).order_by("source_line"))
        assert len(rows) == 2
        assert {r.source_line_id for r in rows} == {cell1.pk, cell2.pk}
        assert {r.prescription_id for r in rows} == {s.squat.pk}

    def test_bare_load_writes_a_partial_set(self, client):
        # ``225`` alone: load, no reps — still a set (pinned in the parser
        # corpus, plan §3).
        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225")
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(source_line=cell)
        assert row.load == "225"
        assert row.reps == ""


# -- collision between the two write paths -----------------------------------


class TestStructuredAndFreeformCollision:
    def test_freeform_then_structured_save_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        parsed_pk = LoggedSet.objects.get(source_line=cell).pk

        resp = log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "8",
                    }
                ]
            },
        )
        assert resp.status_code == 200

        log = the_log(s.session, s.athlete)
        # The parsed set survives untouched...
        parsed = LoggedSet.objects.get(pk=parsed_pk)
        assert parsed.source_line_id == cell.pk
        assert parsed.load == "225"
        # ...the structured set was created alongside it, no duplicate.
        structured = LoggedSet.objects.get(source_line__isnull=True, session_log=log)
        assert structured.prescription_id == s.rdl.pk
        assert LoggedSet.objects.filter(session_log=log).count() == 2

    def test_structured_then_freeform_write_survives(self, client):
        s = seed()
        client.force_login(s.athlete)
        log_post(
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
        log = the_log(s.session, s.athlete)
        structured_pk = LoggedSet.objects.get(
            session_log=log, source_line__isnull=True
        ).pk

        resp = write_cell(client, s.session, s.rdl, 1, "80 x 8")
        assert resp.status_code == 200

        # The structured set is untouched by the freeform write...
        structured = LoggedSet.objects.get(pk=structured_pk)
        assert structured.prescription_id == s.squat.pk
        # ...and the new parsed set was added alongside it.
        cell = sub_cell(s.rdl, 1)
        parsed = LoggedSet.objects.get(source_line=cell)
        assert parsed.load == "80"
        assert parsed.reps == "8"
        assert LoggedSet.objects.filter(session_log=log).count() == 2

    def test_structured_save_progress_does_not_wipe_parsed_sets(self, client):
        # The critical regression this stage guards against: the structured
        # logger's delete used to be scoped to ``prescription_id__in=trainable
        # cells`` only — and a parsed set's ``prescription`` IS a trainable
        # line-0 cell, so an unrelated "Save progress" would silently wipe it.
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        assert LoggedSet.objects.filter(source_line=cell).exists()

        # A "Save progress" that doesn't even mention the squat.
        resp = log_post(client, s.session, {"sets": [], "status": "pending"})
        assert resp.status_code == 200
        assert LoggedSet.objects.filter(source_line=cell).exists()


# -- SessionLog status --------------------------------------------------------


class TestSessionLogStatus:
    def test_new_parsed_set_creates_a_pending_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.PENDING

    def test_cell_write_never_downgrades_a_done_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        # The structured logger's default status is DONE.
        log_post(
            client,
            s.session,
            {
                "sets": [
                    {
                        "prescription": s.rdl.pk,
                        "set_number": 1,
                        "reps": "8",
                        "load": "80",
                        "rpe": "8",
                    }
                ]
            },
        )
        log = the_log(s.session, s.athlete)
        assert log.status == SessionLog.Status.DONE

        write_cell(client, s.session, s.squat, 1, "225 x 5")
        log.refresh_from_db()
        assert log.status == SessionLog.Status.DONE

    def test_cell_write_reuses_the_newest_existing_log(self, client):
        # Mirrors athlete_log_session's ``-created_at`` newest-first lookup —
        # a stray second log row (however it happened) must not fork a new,
        # unrelated log with a fresh PENDING status.
        s = seed()
        first = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.DONE
        )
        newest = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, status=SessionLog.Status.PENDING
        )
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")

        assert (
            SessionLog.objects.filter(session=s.session, athlete=s.athlete).count() == 2
        )
        cell = sub_cell(s.squat, 1)
        row = LoggedSet.objects.get(source_line=cell)
        assert row.session_log_id == newest.pk
        first.refresh_from_db()
        assert first.sets.count() == 0


# -- tolerance guard -----------------------------------------------------------


class TestToleranceGuard:
    def test_parse_error_never_breaks_the_response_or_loses_text(
        self, client, monkeypatch
    ):
        def boom(_text):
            raise RuntimeError("boom")

        monkeypatch.setattr("store_project.meso.views.parse_performed", boom)

        s = seed()
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200

        cell = sub_cell(s.squat, 1)
        assert cell.text == "225 x 5"
        # No set could be derived (the parse blew up), but the write itself
        # is unharmed.
        assert not LoggedSet.objects.filter(source_line=cell).exists()

    def test_parse_error_is_logged(self, client, monkeypatch, caplog):
        def boom(_text):
            raise RuntimeError("boom")

        monkeypatch.setattr("store_project.meso.views.parse_performed", boom)

        s = seed()
        client.force_login(s.athlete)
        with caplog.at_level("ERROR", logger="store_project.meso.views"):
            write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert any(
            "boom" in r.message or "parse" in r.message.lower() for r in caplog.records
        )

    # NOTE: the *database*-error half of the tolerance guard — proving the
    # savepoint in ``_upsert_parsed_set`` keeps a failed upsert from rolling
    # back the athlete's cell text — cannot be tested here. Both tests above
    # raise a plain Python exception, which leaves the connection healthy, so
    # they pass with or without the savepoint. Only a genuinely aborted
    # transaction discriminates, and SQLite doesn't abort. That test lives in
    # ``test_parse_at_commit_postgres.py`` and runs in the Postgres CI job.
