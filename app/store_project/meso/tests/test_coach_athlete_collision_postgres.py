"""PostgreSQL-only: the coach and the athlete both add a NEW line 1 at once (#709).

``cell_line_write`` (coach, ``intent: "new"``) and ``athlete_cell_write``
(athlete, ``new: true``) each take the Plan row lock, then the Session row
lock, BEFORE reading the line. The second writer therefore queues, then finds
the first's committed text on line 1 and lands on the next free line instead of
overwriting it. Both texts survive, each with its own authorship.

The pause hook runs AFTER the first writer has locked and read the line, and
before it commits, so the second request genuinely contends. (A hook placed
after a lazy queryset would pause before the read; ``.first()`` is eager, and
the hook functions below are called after those reads.)

Postgres-only because ``select_for_update`` is a no-op on SQLite and in-memory
SQLite test databases are not shared across threads. Run locally with::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/test_709_race \
        uv run pytest app/store_project/meso/tests/test_coach_athlete_collision_postgres.py -v
"""

import json

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from store_project.meso import views
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.tests.test_coach_logs_709 import start
from store_project.meso.tests.test_double_submit_postgres import _http_race
from store_project.meso.tests.test_parse_at_commit import seed

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="row locks and cross-thread DB sharing need PostgreSQL",
    ),
]

#: Coach path: `record_plan_action` is called after both locks and after the
#: line was read and judged free, and before any write. Pausing "before" it
#: holds the coach mid-transaction with a stale-able decision. The athlete
#: path never calls it, so only the coach thread can pause here.
COACH_AFTER_READ = (views, "record_plan_action", "before")
#: Athlete path: `_upsert_parsed_set` runs after the line was read, claimed
#: and saved, still uncommitted. (The coach also calls it, but only after its
#: own pause point, and only when it is the first thread this hook matters for.)
ATHLETE_AFTER_WRITE = (views, "_upsert_parsed_set", "before")


def _coach_request(s, text="5 @ 225"):
    client = Client()
    client.force_login(s.coach)
    return (
        client,
        reverse(
            "meso:api_cell_line_write",
            kwargs={"plan_id": s.plan.pk, "slot_id": s.squat.exercise_slot.pk},
        ),
        json.dumps({"week_id": s.week.pk, "line": 1, "text": text, "intent": "new"}),
        "application/json",
    )


def _athlete_request(s, text="225 x 5"):
    client = Client()
    client.force_login(s.athlete)
    return (
        client,
        reverse("meso:athlete_cell_write", kwargs={"pk": s.session.pk}),
        json.dumps({"exercise_id": s.squat.pk, "line": 1, "text": text, "new": True}),
        "application/json",
    )


def _assert_both_landed(s, coach_text, athlete_text, coach_line, athlete_line):
    cells = {
        c.line: c
        for c in Prescription.objects.filter(
            exercise_slot=s.squat.exercise_slot, week=s.week, line__gte=1
        )
    }
    assert sorted(cells) == [1, 2]
    coach_cell, athlete_cell = cells[coach_line], cells[athlete_line]
    assert (coach_cell.text, coach_cell.entered_by_coach) == (coach_text, True)
    assert (athlete_cell.text, athlete_cell.entered_by_coach) == (athlete_text, False)
    assert athlete_cell.athlete_authored is True
    assert LoggedSet.objects.filter(session_log__session=s.session).count() == 2


def test_coach_first_then_athlete_both_new_line_1():
    s = seed()
    start(s)
    errors = [None, None]
    responses = _http_race(
        [_coach_request(s), _athlete_request(s)], [COACH_AFTER_READ], errors=errors
    )
    assert errors == [None, None]
    assert [r.status_code for r in responses] == [200, 200]
    assert "relocated_from" not in responses[0].json()
    assert responses[1].json()["relocated_from"] == 1
    _assert_both_landed(s, "5 @ 225", "225 x 5", coach_line=1, athlete_line=2)


def test_athlete_first_then_coach_both_new_line_1():
    s = seed()
    start(s)
    errors = [None, None]
    responses = _http_race(
        [_athlete_request(s), _coach_request(s)], [ATHLETE_AFTER_WRITE], errors=errors
    )
    assert errors == [None, None]
    assert [r.status_code for r in responses] == [200, 200]
    assert "relocated_from" not in responses[0].json()
    assert responses[1].json()["relocated_from"] == 1
    _assert_both_landed(s, "5 @ 225", "225 x 5", coach_line=2, athlete_line=1)
