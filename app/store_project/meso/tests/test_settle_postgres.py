r"""PostgreSQL-only regression tests for the settle sweep's row lock (5b).

``settle.settle_log`` takes the SAME ``Session.objects.select_for_update()``
lock both athlete write paths take first (``athlete_log_session``
~views.py:1378, ``athlete_cell_write`` ~views.py:1693) — and takes it BEFORE
re-reading the log, so a concurrent blur/save and the sweep can never both act
on stale state (see ``settle.py``'s module docstring for the full contract).

**Why this file exists separately.** ``select_for_update`` is a documented
no-op on SQLite, and the default in-memory SQLite test database doesn't even
share rows across threads/connections — each thread gets its own private
database, so two threads can never contend for the same row lock at all. A
missing lock and a working one would look identical there: a test for lock
*contention* is simply not expressible on SQLite. This is the same
SQLite-vs-Postgres blind spot as ``test_migration_0040_pending_triggers.py``
(deferred constraints) and ``test_parse_at_commit_postgres.py`` (transaction-
abort semantics) — different mechanism each time, same lesson: some of what
prod depends on only exists on Postgres.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \\
        uv run pytest app/store_project/meso/tests/test_settle_postgres.py -v
"""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.db import transaction
from django.utils import timezone

from store_project.meso import settle
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import the_log
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason=(
            "select_for_update is a no-op on SQLite, and in-memory SQLite "
            "test databases aren't even shared across threads/connections — "
            "a missing lock would look identical to a working one here."
        ),
    ),
]


def test_settle_log_blocks_on_a_concurrently_held_session_lock(client):
    """A blur/save holding the Session lock must block the sweep, not race it."""
    s = seed()
    client.force_login(s.athlete)
    write_cell(client, s.session, s.squat, 1, "100 x 5")
    log = the_log(s.session, s.athlete)
    cutoff = timezone.now() - timedelta(hours=24)
    SessionLog.objects.filter(pk=log.pk).update(
        last_activity_at=cutoff - timedelta(minutes=1)
    )

    session_pk = s.session.pk
    holding = threading.Event()
    release = threading.Event()

    def hold_then_bump_and_commit():
        # Thread A: stands in for a concurrent blur/save — takes the exact
        # lock those paths take first, then (while still holding it) bumps
        # last_activity_at past the cutoff, simulating the athlete acting
        # while the sweep waits, and only then commits (releasing the lock).
        try:
            with transaction.atomic():
                Session.objects.select_for_update().filter(pk=session_pk).first()
                holding.set()
                release.wait(timeout=5)
                SessionLog.objects.filter(pk=log.pk).update(
                    last_activity_at=timezone.now()
                )
        finally:
            connection.close()

    holder = threading.Thread(target=hold_then_bump_and_commit)
    holder.start()
    assert holding.wait(timeout=5), "the holder thread never acquired the lock"

    result = {}

    def try_settle():
        # Thread B: the sweep, racing the holder for the same Session row.
        try:
            result["settled"] = settle.settle_log(log.pk, cutoff=cutoff)
        finally:
            connection.close()

    settler = threading.Thread(target=try_settle)
    settler.start()
    settler.join(timeout=0.5)
    assert settler.is_alive(), "settle_log did not block on the held session lock"

    release.set()
    settler.join(timeout=5)
    holder.join(timeout=5)
    assert not settler.is_alive()
    assert not holder.is_alive()

    # By the time settle_log's re-read runs, the bump moved last_activity_at
    # past the cutoff — its own under-lock re-check must catch that and back
    # off, exactly as the SQLite-simulated version of this scenario does in
    # test_settle.py::TestUnderLockRecheck.
    assert result["settled"] is False
    log.refresh_from_db()
    assert log.status == SessionLog.Status.PENDING
