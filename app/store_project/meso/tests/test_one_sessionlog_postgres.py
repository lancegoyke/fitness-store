"""PostgreSQL-only: two concurrent FIRST writes for one pair make ONE log (#699).

Both athlete write paths (``athlete_cell_write`` and ``athlete_log_session``)
read ``newest_session_logs`` under the ``Session`` row lock and create the log
when none exists. A first typed line racing "Finish session" (or a second
line) must end with exactly one ``SessionLog`` carrying both writes, and
neither request may 500. The constraint ``meso_sessionlog_one_per_athlete_
session`` backs this up for writers that do not take the lock.

Postgres-only because ``select_for_update`` is a no-op on SQLite and in-memory
SQLite test databases are not shared across threads. Run locally with::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/test_meso_699 \
        uv run pytest app/store_project/meso/tests/test_one_sessionlog_postgres.py -v
"""

import json

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_double_submit_postgres import _http_race
from store_project.meso.tests.test_parse_at_commit import seed

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="row locks and cross-thread DB sharing need PostgreSQL",
    ),
]

#: A pause right after the view reads "is there a log yet?" — still inside the
#: Session lock, so the second request queues on the lock and then reads the
#: first one's committed log.
AFTER_LOG_LOOKUP = (views, "newest_session_logs", "after")


def _cell_request(s, line, text):
    client = Client()
    client.force_login(s.athlete)
    return (
        client,
        reverse("meso:athlete_cell_write", kwargs={"pk": s.session.pk}),
        json.dumps({"exercise_id": s.squat.pk, "line": line, "text": text}),
        "application/json",
    )


def _finish_request(s):
    client = Client()
    client.force_login(s.athlete)
    return (
        client,
        reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
        json.dumps({"status": "done"}),
        "application/json",
    )


def test_a_first_line_and_finish_session_share_one_log():
    s = seed()
    errors = [None, None]
    responses = _http_race(
        [_cell_request(s, 1, "225 x 5"), _finish_request(s)],
        [AFTER_LOG_LOOKUP],
        errors=errors,
    )
    assert errors == [None, None]
    assert [r.status_code for r in responses] == [200, 200]
    log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
    assert log.status == SessionLog.Status.DONE
    row = LoggedSet.objects.get()
    assert row.session_log_id == log.pk
    assert (row.load, row.reps) == ("225", "5")


def test_two_first_lines_share_one_log():
    s = seed()
    errors = [None, None]
    responses = _http_race(
        [_cell_request(s, 1, "225 x 5"), _cell_request(s, 2, "135 x 8")],
        [AFTER_LOG_LOOKUP],
        errors=errors,
    )
    assert errors == [None, None]
    assert [r.status_code for r in responses] == [200, 200]
    log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
    assert sorted(LoggedSet.objects.values_list("session_log_id", flat=True)) == [
        log.pk,
        log.pk,
    ]
