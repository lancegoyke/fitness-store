r"""PostgreSQL-only regression test for the stray-cell purge's row lock (#584).

``history.restore_plan_snapshot``'s stray-cell purge spares any cell athlete
data points at, via three ``LoggedSet`` pointers checked through
``history._cells_athlete_data_points_at`` plus ``athlete_authored``. Before
this fix, that check ran as part of ``QuerySet.delete()`` — a plain
SELECT-then-DELETE with **no row lock** on its candidates. A ``LoggedSet``
committed by a concurrent athlete write, in the gap between the purge's
qualifying SELECT and its DELETE, was invisible to every one of those checks,
and the cell was deleted out from under it anyway: for ``prescription``/
``source_line`` (real FKs) that surfaced as a COMMIT-time deferred constraint
violation (a 500 on the coach's undo); for ``reclaimed_line``
(``db_constraint=False``, #541) nothing stopped it and the hint was left
dangling.

The fix (``history.py``) qualifies the purge's candidates under
``select_for_update(of=("self",))`` and re-checks every spare test only after
that lock is held, not before. That works even though the athlete's own write
path
(``athlete_cell_write``'s plain ``cell.save(update_fields=...)``) never
takes an EXPLICIT lock of its own: on PostgreSQL, an UPDATE (or, here, the
athlete's `Prescription` text write standing in for it) implicitly holds a
row-level lock on the row it touches for the rest of its transaction, and
`select_for_update` in another transaction still has to wait on that implicit
lock — the fix doesn't need the writer's cooperation, only the reader's
discipline.

**Why this file exists separately.** ``select_for_update`` is a documented
no-op on SQLite, and the default in-memory SQLite test database doesn't even
share rows across threads/connections — each thread gets its own private
database, so two threads can never contend for the same row lock at all. A
missing lock and a working one would look identical there: a test for lock
*contention* is simply not expressible on SQLite. Same blind spot as
``test_settle_postgres.py`` and ``test_billing_webhook_postgres.py``.

**How the interleaving is forced, and how it's PROVEN, not assumed.** Thread A
(standing in for the athlete's write) takes the row lock the real path would
take implicitly, signals a ``threading.Event`` the moment it holds it, and
then blocks on a SECOND event before it inserts the ``LoggedSet`` and
commits. Thread B (the coach's undo, driven through the real
``api_plan_undo`` endpoint) reaches the purge and blocks trying to acquire
the same lock. The main thread does not just assume B is blocked after some
fixed sleep — a slow CI runner or a GC pause could make that flaky in either
direction (a sleep too short reports "not blocked" when it would have
blocked with more time; a sleep long enough to be safe makes the suite slower
for no reason). Instead it polls PostgreSQL's own bookkeeping
(``pg_stat_activity.wait_event_type = 'Lock'``, scoped to
``current_database()``) from a THIRD connection until it reports a backend
actually waiting on a lock, with a timeout — i.e. it asks Postgres to confirm
the block is real rather than inferring it from timing. Only then does the
main thread release thread A, which inserts the ``LoggedSet`` and commits,
unblocking thread B's lock acquisition — at which point B's re-check must see
the just-committed row and spare the cell.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \
        uv run pytest app/store_project/meso/tests/test_undo_purge_postgres.py -v
"""

import threading
import time

import pytest
from django.db import connection
from django.db import transaction
from django.test import Client
from django.urls import reverse

from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_undo_spares_logged_sets import _write_line

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


def _wait_until_a_backend_is_lock_blocked(timeout=5.0, interval=0.02):
    """Poll ``pg_stat_activity`` until some backend is genuinely waiting on a lock.

    Uses the calling (main) thread's own connection — a THIRD connection,
    distinct from both worker threads' — so this is Postgres itself
    confirming the block, not a guess based on elapsed time. Returns
    ``True`` once seen, ``False`` on timeout.
    """
    deadline = time.monotonic() + timeout
    with connection.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
            (blocked,) = cur.fetchone()
            if blocked > 0:
                return True
            time.sleep(interval)
    return False


class TestPurgeSparesACellALoggedSetCommitsMidUndo:
    """The forced interleaving: a set lands ON the row the purge is about to delete."""

    def test_the_cell_and_its_logged_set_both_survive_the_race(self, client):
        s = seed()

        # A sub-line cell the purge WOULD otherwise remove: created after the
        # snapshot the coach's undo is about to restore (`_write_line` records
        # a `PlanAction` capturing the state WITHOUT this cell, then creates
        # it), with no athlete data pointing at it yet.
        client.force_login(s.coach)
        resp = _write_line(
            client, s.plan, s.squat.exercise_slot, s.week, line=1, text="threatened"
        )
        assert resp.status_code == 200, resp.content
        stray_cell = sub_cell(s.squat, 1)
        stray_pk = stray_cell.pk

        log = SessionLog.objects.create(session=s.session, athlete=s.athlete)

        holding = threading.Event()
        release = threading.Event()
        athlete_errors = []

        def athlete_write_then_log_and_commit():
            # Thread A: stands in for the athlete's write landing mid-undo.
            # The UPDATE below writes the same row `athlete_cell_write`'s
            # `cell.save(update_fields=[...])` writes — no explicit lock of
            # its own, relying (like the real path does) on Postgres's
            # implicit per-row UPDATE lock to hold the row for the rest of
            # this transaction.
            #
            # It writes `text` ONLY, deliberately leaving `athlete_authored`
            # alone. That flag is still part of the purge's candidate filter,
            # so flipping it here would spare the cell on its own and this
            # test would pass whether or not the `LoggedSet` re-check under
            # the lock works — the one thing it exists to prove. Text-only is
            # also a real shape: `athlete_log_session`'s reclaim writes a
            # sub-line's text while leaving it coach-owned (#541), with the
            # set's link the only thing naming the cell. With the flag out of
            # play, the ONLY thing that can save this cell is the purge
            # seeing the just-committed set.
            try:
                with transaction.atomic():
                    Prescription.objects.filter(pk=stray_pk).update(text="225 x 5")
                    holding.set()
                    assert release.wait(timeout=5), (
                        "the main thread never released the athlete's commit"
                    )
                    # Both pointers set, like the real `_upsert_parsed_set`
                    # write: `source_line` is the sub-line the text was
                    # parsed from (the row under threat here), `prescription`
                    # is the exercise's line-0 cell.
                    LoggedSet.objects.create(
                        session_log=log,
                        prescription_id=s.squat.pk,
                        source_line_id=stray_pk,
                        set_number=1,
                        reps="5",
                        load="225",
                    )
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                athlete_errors.append(exc)
            finally:
                connection.close()

        holder = threading.Thread(target=athlete_write_then_log_and_commit)
        holder.start()
        assert holding.wait(timeout=5), "the athlete thread never acquired the row lock"

        undo_client = Client()
        undo_client.force_login(s.coach)
        result = {}

        def run_the_coach_undo():
            # Thread B: the real endpoint, not a bare call into
            # `restore_plan_snapshot` — this exercises `api_plan_undo`'s own
            # `Plan` lock + transaction wrapping too.
            try:
                resp = undo_client.post(
                    reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk}),
                    content_type="application/json",
                )
                result["status_code"] = resp.status_code
                result["content"] = resp.content
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                result["error"] = exc
            finally:
                connection.close()

        undoer = threading.Thread(target=run_the_coach_undo)
        undoer.start()

        assert _wait_until_a_backend_is_lock_blocked(timeout=5.0), (
            "no backend was ever reported lock-blocked by pg_stat_activity — "
            "the undo either raced ahead of the athlete's write instead of "
            "blocking on it, or never reached the purge at all"
        )
        # The undo thread must still be running at this point — it found the
        # lock held and is waiting, not finished (which would mean it never
        # blocked in the first place, e.g. because it 409ed before reaching
        # the purge).
        assert undoer.is_alive(), (
            "the undo thread finished before being released — it never "
            "actually blocked on the athlete's lock"
        )

        release.set()
        undoer.join(timeout=10)
        holder.join(timeout=10)
        assert not undoer.is_alive(), "the undo thread never finished"
        assert not holder.is_alive(), "the athlete thread never finished"

        assert athlete_errors == [], f"the athlete thread raised: {athlete_errors}"
        assert "error" not in result, f"the undo thread raised: {result.get('error')}"
        assert result["status_code"] == 200, (
            f"the undo did not come back 200: {result.get('content')}"
        )

        stray_cell.refresh_from_db()
        assert stray_cell.text == "225 x 5", (
            "the purge deleted (or the restore otherwise overwrote) the cell "
            "the athlete's set was committed against mid-race"
        )
        logged = LoggedSet.objects.get(session_log=log)
        assert logged.source_line_id == stray_pk, (
            "the athlete's logged set lost its link to the sub-line it was parsed from"
        )
        assert logged.prescription_id == s.squat.pk, (
            "the athlete's logged set lost its link to the exercise's line-0 cell"
        )
