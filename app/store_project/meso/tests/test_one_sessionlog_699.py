"""#699 — the database allows one ``SessionLog`` per ``(session, athlete)``.

``meso_sessionlog_one_per_athlete_session`` replaces a race the athlete write
paths only closed with the ``Session`` row lock (and a pair of selectors that
guessed which of two logs was "the" log). These tests pin the constraint, its
name, the lost-race fallback on both athlete write paths (a writer that does
NOT hold the lock inserted first), the admin form error, and the migration.
Portable: runs on SQLite and Postgres.
"""

import importlib
from unittest import mock

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.urls import reverse

from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import cell_post
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import write_cell
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

CONSTRAINT = "meso_sessionlog_one_per_athlete_session"


def _pair_logs(s):
    return SessionLog.objects.filter(session=s.session, athlete=s.athlete)


def _blind_first_read():
    """``views.newest_session_logs`` whose FIRST call misses an existing log.

    Simulates a writer without the Session lock inserting the log between this
    view's read and its create: the view sees "no log", tries to insert, and
    hits the constraint. Later calls are real.
    """
    real = views.newest_session_logs
    calls = []

    def fake(session, athlete, *, status=None):
        calls.append(1)
        if len(calls) == 1:
            return SessionLog.objects.none()
        return real(session, athlete, status=status)

    return mock.patch.object(views, "newest_session_logs", fake)


class TestConstraint:
    def test_the_model_refuses_a_second_log_for_the_pair(self):
        s = seed()
        SessionLog.objects.create(session=s.session, athlete=s.athlete)
        with pytest.raises(IntegrityError), transaction.atomic():
            SessionLog.objects.create(session=s.session, athlete=s.athlete)
        assert _pair_logs(s).count() == 1

    def test_the_constraint_is_named(self):
        names = {c.name for c in SessionLog._meta.constraints}
        assert CONSTRAINT in names

    def test_another_athlete_or_session_is_not_blocked(self):
        s = seed()
        SessionLog.objects.create(session=s.session, athlete=s.athlete)
        SessionLog.objects.create(session=s.session, athlete=UserFactory())

    def test_create_for_pair_returns_the_existing_log(self):
        s = seed()
        first = SessionLog.objects.create_for_pair(s.session, s.athlete)
        with transaction.atomic():
            again = SessionLog.objects.create_for_pair(
                s.session, s.athlete, notes="ignored"
            )
        assert again.pk == first.pk
        assert again.notes == ""
        assert _pair_logs(s).count() == 1


class TestLostRaceFallback:
    def test_cell_write_adopts_the_existing_log(self, client):
        s = seed()
        existing = SessionLog.objects.create(session=s.session, athlete=s.athlete)
        client.force_login(s.athlete)
        with _blind_first_read():
            resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200
        assert _pair_logs(s).count() == 1
        row = LoggedSet.objects.get()
        assert row.session_log_id == existing.pk
        assert (row.load, row.reps) == ("225", "5")

    def test_log_session_adopts_the_existing_log(self, client):
        s = seed()
        existing = SessionLog.objects.create(
            session=s.session, athlete=s.athlete, notes=""
        )
        client.force_login(s.athlete)
        with _blind_first_read():
            resp = log_post(client, s.session, {"status": "done", "notes": "felt good"})
        assert resp.status_code == 200
        assert resp.json()["log"]["id"] == existing.pk
        assert _pair_logs(s).count() == 1
        existing.refresh_from_db()
        assert existing.status == SessionLog.Status.DONE
        assert existing.notes == "felt good"
        assert existing.date is not None

    def test_log_session_keeps_an_existing_logs_sticky_done_and_date(self, client):
        import datetime

        s = seed()
        existing = SessionLog.objects.create(
            session=s.session,
            athlete=s.athlete,
            status=SessionLog.Status.DONE,
            date=datetime.date(2026, 1, 2),
        )
        client.force_login(s.athlete)
        with _blind_first_read():
            resp = log_post(client, s.session, {"status": "pending"})
        assert resp.status_code == 200
        existing.refresh_from_db()
        assert existing.status == SessionLog.Status.DONE
        assert existing.date == datetime.date(2026, 1, 2)

    def test_first_cell_write_still_creates_the_log(self, client):
        s = seed()
        client.force_login(s.athlete)
        resp = cell_post(
            client, s.session, {"exercise_id": s.squat.pk, "line": 1, "text": "100 x 5"}
        )
        assert resp.status_code == 200
        assert _pair_logs(s).count() == 1


class TestAdmin:
    def test_adding_a_duplicate_pair_is_a_form_error_not_a_500(self, client):
        s = seed()
        SessionLog.objects.create(session=s.session, athlete=s.athlete)
        client.force_login(UserFactory(is_staff=True, is_superuser=True))
        resp = client.post(
            reverse("admin:meso_sessionlog_add"),
            {
                "session": s.session.pk,
                "athlete": s.athlete.pk,
                "status": "pending",
                "notes": "",
                "sets-TOTAL_FORMS": "0",
                "sets-INITIAL_FORMS": "0",
                "sets-MIN_NUM_FORMS": "0",
                "sets-MAX_NUM_FORMS": "1000",
            },
        )
        assert resp.status_code == 200
        assert "already exists" in resp.content.decode()
        assert _pair_logs(s).count() == 1


class TestMigration:
    def test_0061_adds_the_named_constraint_with_no_data_step(self):
        mod = importlib.import_module(
            "store_project.meso.migrations.0061_sessionlog_one_per_athlete_session_699"
        )
        ops = mod.Migration.operations
        assert [type(op).__name__ for op in ops] == ["AddConstraint"]
        assert ops[0].constraint.name == CONSTRAINT
        assert ops[0].model_name == "sessionlog"
