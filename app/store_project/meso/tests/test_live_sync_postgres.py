"""PostgreSQL-only: a grid read overlapping a write never pairs a NEW stamp with OLD rows.

#709 PR 2 / #718. ``serialize_mesocycle_grid`` reads ``Plan.sync_version`` FIRST,
then the rows. A write that commits in between can only leave the payload
holding NEW rows under an OLD stamp (the next poll refetches: harmless). The
reverse, OLD rows under a NEW stamp, tells a client it is current about a grid
that predates a write it already adopted: the #718 revert.

The race: a worker thread serializes the grid and parks right after it has read
every grid row (at ``week_readouts``, which runs after the cell reads and before
the return). While it is parked the main thread commits a coach write. When the
worker resumes, its stamp must not be newer than its rows. If the stamp were
read last (after the rows), it would read the post-write value and this fails:
mutation-proved by moving the ``read_plan_sync`` call into the returned dict.

Postgres-only: in-memory SQLite test databases aren't shared across threads, so
a second connection could never see (or race) the first's rows. Run locally::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/test_709s_pg \
        uv run pytest app/store_project/meso/tests/test_live_sync_postgres.py -v
"""

import threading
from unittest import mock

import pytest
from django.db import connection
from django.db import connections

from store_project.meso import models
from store_project.meso import serializers
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import seed

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="needs a second connection that shares the database",
    ),
]


def _cue_texts(grid, s):
    rows = [r for d in grid["days"] for r in d["rows"]]
    row = next(r for r in rows if r["exercise_slot_id"] == s.squat.exercise_slot_id)
    cell = row["cells"][str(s.week.pk)]
    return [line["text"] for line in cell["lines"]]


def test_the_stamp_is_never_newer_than_the_rows(client):
    s = seed()
    mesocycle = models.Mesocycle.objects.get(pk=s.meso.pk)
    stamp_before = models.read_plan_sync(s.plan.pk)

    rows_read = threading.Event()
    write_done = threading.Event()
    real_readouts = serializers.week_readouts
    out = {}

    def parked_readouts(*args, **kwargs):
        # Every grid row has been read by now; hold the worker here.
        rows_read.set()
        assert write_done.wait(10)
        return real_readouts(*args, **kwargs)

    def worker():
        try:
            out["grid"] = serializers.serialize_mesocycle_grid(mesocycle)
        except Exception as exc:  # surfaced by the main thread's assert
            out["error"] = exc
        finally:
            connections.close_all()

    with mock.patch.object(serializers, "week_readouts", parked_readouts):
        thread = threading.Thread(target=worker)
        thread.start()
        assert rows_read.wait(10)
        client.force_login(s.coach)
        resp = coach_write(client, s, "Pause at the bottom")
        assert resp.status_code == 200, resp.content
        write_done.set()
        thread.join(15)
    assert "error" not in out, out.get("error")

    stamp_after = models.read_plan_sync(s.plan.pk)
    assert stamp_after == stamp_before + 1
    grid = out["grid"]
    # The write committed AFTER this read's rows, so the rows are the old ones:
    # the stamp must be the old one too (never `stamp_after`).
    assert grid["sync_v"] == stamp_before
    assert "Pause at the bottom" not in _cue_texts(grid, s)
