r"""PostgreSQL-only regression tests for the Session/Plan lock cycle (#562).

**The cycle.** ``views.athlete_cell_write`` writes the athlete's freeform
sub-line cell. Before this fix it opened by locking the ``Session`` row
(``select_for_update``), read/upserted the cell, and only then — via
``_touch_plan`` — issued an ``UPDATE`` on the ``Plan`` row (an implicit
exclusive row lock): Session, then Plan. ``views.api_plan_undo`` and
``api_plan_redo`` do the opposite: they lock the ``Plan`` row
(``select_for_update``) as their first statement, then
``history.restore_plan_snapshot`` ``.save()``s every snapshotted ``Session``
row of the plan: Plan, then Session. An athlete blurring a line while their
coach undoes on the very same plan could hold each half of that cycle at
once — the athlete's transaction holding Session and waiting on Plan, the
coach's holding Plan and waiting on Session. PostgreSQL's deadlock detector
finds the cycle and aborts one side, which 500s: either the athlete's line
save or the coach's undo.

**The fix under test.** ``athlete_cell_write`` now takes
``Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()`` as
the FIRST statement inside its ``transaction.atomic()`` block, above the
``Session`` lock — Plan, then Session, matching the app-wide order written
down in ``docs/meso/decisions.md`` ("Row-lock order"). The same commit also
hoists a ``Plan`` lock in ``cell_line_write`` and ``batch_apply`` for two
*different* inversions (#558, #559); this file is scoped to the
``athlete_cell_write`` / ``api_plan_undo`` cycle only.

**Why this is only expressible on PostgreSQL.** ``select_for_update`` is a
documented no-op on SQLite, and the default in-memory SQLite test database
doesn't even share rows across threads/connections — each thread gets its own
private database, so two threads can never contend for the same row lock at
all, let alone deadlock. A missing lock, a correctly-ordered lock, and a
wrongly-ordered lock all look identical there. Same blind spot as
``test_undo_purge_postgres.py``, ``test_billing_webhook_postgres.py``, and
``test_settle_postgres.py``.

**How the interleaving is forced, and how it's PROVEN, not assumed.** Two
hooks, one per side of the cycle, each a small wrapper around a function the
real code path already calls by its bare (module-global) name:

* ``views._touch_plan`` — the athlete side's *only* remaining touch of the
  ``Plan`` row inside ``athlete_cell_write`` (the fix moved the actual lock
  acquisition above it; this call is now just the ``modified`` bump). The
  wrapper sets ``a_reached``, waits on ``a_go`` (with a timeout), then calls
  through.
* ``views.restore_plan_snapshot`` — **not** ``history.restore_plan_snapshot``.
  ``views.py`` does ``from .history import restore_plan_snapshot`` at import
  time, which binds the name into ``views``'s OWN module namespace;
  ``api_plan_undo``/``api_plan_redo`` call the bare name, which Python
  resolves against ``views.__dict__`` at call time. Patching
  ``history.restore_plan_snapshot`` instead would silently do nothing here —
  the already-bound reference in ``views`` wouldn't change. The wrapper sets
  ``b_reached``, waits on ``b_go`` (with a timeout), then calls through.

Thread A (the athlete's real ``athlete_cell_write`` request) is started
first; the main thread waits for ``a_reached`` — which must always fire,
fix or no fix, since ``_touch_plan`` is the one line every non-no-op blur
reaches. Thread B (the coach's real ``api_plan_undo`` request) is started
next.

What happens after that is the actual test of the fix, and it does NOT
assume which world (fixed or broken) is running:

* **Fixed world:** the athlete's transaction already holds the ``Plan`` row
  (``FOR NO KEY UPDATE``, taken at the very top of ``athlete_cell_write``,
  before ``_touch_plan`` is ever reached) by the time thread B starts. B's
  own ``Plan.objects.select_for_update()`` (plain ``FOR UPDATE`` — which DOES
  conflict with a held ``FOR NO KEY UPDATE``, per ``docs/meso/decisions.md``)
  blocks before B ever reaches ``restore_plan_snapshot`` — so ``b_reached``
  is never going to fire. The main thread does not just wait out a fixed
  sleep and declare victory (a slow CI runner could make that flaky in
  either direction); it polls PostgreSQL's own bookkeeping
  (``pg_stat_activity.wait_event_type = 'Lock'``, from a THIRD connection)
  until Postgres itself confirms some backend is genuinely blocked on a
  lock, with a timeout. Only that — Postgres's own word, not an elapsed
  clock — counts as proof the block is real.
* **Broken world (Session-then-Plan restored):** the athlete's transaction
  holds only the ``Session`` row at the point ``_touch_plan`` fires — it
  hasn't touched ``Plan`` yet, since the wrapper stands in for
  ``_touch_plan``'s own (not-yet-run) ``UPDATE``. B's ``Plan`` lock is
  therefore free, B sails through to ``restore_plan_snapshot``, and
  ``b_reached`` fires almost immediately.

Either way, the main thread releases both ``a_go`` and ``b_go`` once it has
resolved that question, joins both threads, and asserts neither raised and
both answered 200. In the fixed world that's simply true. In the broken
world, releasing both at that point closes the real cycle for real: thread A
resumes into ``_touch_plan``'s actual ``UPDATE`` on ``Plan`` (blocked — B
holds it), thread B resumes into ``restore_plan_snapshot``'s actual
``session.save()`` (blocked — A holds it), and PostgreSQL's deadlock
detector aborts whichever side it picks as the victim within its
``deadlock_timeout`` (1s by default) — surfacing as a
``django.db.utils.OperationalError: deadlock detected`` raised out of
whichever thread's ``client.post()`` call that was. The test captures each
thread's exception explicitly (rather than letting a raise inside a
``threading.Thread`` target vanish silently) and asserts both are ``None``,
naming ``deadlock detected`` in the failure message so a reader hits the
right issue immediately.

A second, cheap test below pins the SQL order directly rather than its
consequence, via ``CaptureQueriesContext``: a future refactor that keeps both
locks but swaps their order would not necessarily deadlock on any given test
run (deadlocks need real concurrency), so relying on the race test alone to
catch a reordering would be flaky-by-construction. The order test can't be —
it inspects the emitted SQL of a single, ordinary, single-threaded request.

Run locally against the dedicated Postgres database for this fix::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/lock562 \
        uv run pytest app/store_project/meso/tests/test_athlete_undo_lock_order_postgres.py -v
"""

import threading
import time

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from store_project.meso import views
from store_project.meso.models import PlanAction
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell
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


def _undo(client, plan):
    return client.post(
        reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk}),
        content_type="application/json",
    )


class TestAthleteWriteAndCoachUndoDoNotDeadlock:
    """The race the issue describes, forced at the lock rather than a sleep."""

    def test_the_athletes_line_save_and_the_coachs_undo_both_answer_200(
        self, monkeypatch
    ):
        s = seed()

        # One coach edit of the squat's own line 0 — `cell_line_write` calls
        # `record_plan_action` BEFORE it overwrites the text, so this both
        # (a) puts exactly one PlanAction on the undo stack, whose snapshot
        # captures the plan's Week/SessionSlot/ExerciseSlot/Session rows
        # (including `s.session`, the plan's only one) as they stood before
        # this edit, and (b) gives the undo something real to put back:
        # `s.squat`'s text reverts from this edit's text to whatever `seed()`
        # originally gave it.
        original_squat_text = s.squat.text
        setup_client = Client()
        setup_client.force_login(s.coach)
        resp = _write_line(
            setup_client,
            s.plan,
            s.squat.exercise_slot,
            s.week,
            line=0,
            text="3 x 5, 225",
        )
        assert resp.status_code == 200, resp.content
        assert PlanAction.objects.filter(
            plan=s.plan, stack=PlanAction.Stack.UNDO
        ).exists(), "the setup edit didn't record an undo action to race against"

        a_reached = threading.Event()
        a_go = threading.Event()
        b_reached = threading.Event()
        b_go = threading.Event()

        real_touch_plan = views._touch_plan

        def touch_plan_then_wait(plan):
            # Thread A's hook. By the time this fires, the fix's Plan lock is
            # ALREADY held (taken at the top of `athlete_cell_write`, above
            # the Session lock) — this call itself is now only the
            # `modified` bump, not an acquisition. Under the pre-fix code
            # this would be the FIRST time the athlete's transaction ever
            # touched the Plan row.
            a_reached.set()
            assert a_go.wait(timeout=10), (
                "main thread never released the athlete's _touch_plan call"
            )
            return real_touch_plan(plan)

        real_restore_plan_snapshot = views.restore_plan_snapshot

        def restore_plan_snapshot_then_wait(plan, snapshot):
            # Thread B's hook — patched on `views`, not on `history`. See
            # the module docstring: `views.py` imports the name with
            # ``from .history import restore_plan_snapshot``, so the name
            # `api_plan_undo`/`api_plan_redo` actually call at runtime lives
            # in `views.__dict__`, not `history.__dict__`.
            b_reached.set()
            assert b_go.wait(timeout=10), (
                "main thread never released the coach's restore_plan_snapshot call"
            )
            return real_restore_plan_snapshot(plan, snapshot)

        athlete_client = Client()
        athlete_client.force_login(s.athlete)
        athlete_result = {}

        def run_athlete_write():
            try:
                resp = write_cell(athlete_client, s.session, s.squat, 1, "225 x 5")
                athlete_result["status_code"] = resp.status_code
                athlete_result["content"] = resp.content
            except Exception as exc:  # pragma: no cover - surfaced via assert below
                athlete_result["error"] = exc
            finally:
                connection.close()

        undo_client = Client()
        undo_client.force_login(s.coach)
        undo_result = {}

        def run_coach_undo():
            try:
                resp = _undo(undo_client, s.plan)
                undo_result["status_code"] = resp.status_code
                undo_result["content"] = resp.content
            except Exception as exc:  # pragma: no cover - surfaced via assert below
                undo_result["error"] = exc
            finally:
                connection.close()

        monkeypatch.setattr(views, "_touch_plan", touch_plan_then_wait)
        monkeypatch.setattr(
            views, "restore_plan_snapshot", restore_plan_snapshot_then_wait
        )

        athlete_thread = threading.Thread(target=run_athlete_write)
        athlete_thread.start()
        assert a_reached.wait(timeout=5), "the athlete thread never reached _touch_plan"

        undo_thread = threading.Thread(target=run_coach_undo)
        undo_thread.start()

        # THE CRUX. Do not require `b_reached` to fire — under the fix it
        # never will (see the module docstring). Give it a short window,
        # then either way move on to proving what actually happened.
        b_reached_promptly = b_reached.wait(timeout=1.0)
        if not b_reached_promptly:
            assert _wait_until_a_backend_is_lock_blocked(timeout=5.0), (
                "b_reached never fired AND no backend was ever reported "
                "lock-blocked by pg_stat_activity — the undo neither "
                "reached restore_plan_snapshot nor blocked acquiring the "
                "Plan lock; the test setup is wrong, not merely 'the fix "
                "works'"
            )
            assert undo_thread.is_alive(), (
                "the undo thread finished without ever reaching "
                "restore_plan_snapshot or blocking on the Plan lock — it "
                "must be doing exactly one of the two"
            )

        a_go.set()
        b_go.set()
        athlete_thread.join(timeout=10)
        undo_thread.join(timeout=10)

        assert not athlete_thread.is_alive(), "the athlete thread never finished"
        assert not undo_thread.is_alive(), "the coach's undo thread never finished"

        assert "error" not in athlete_result, (
            "the athlete's line save raised instead of answering — a "
            "`deadlock detected` here means #562 has regressed (the athlete "
            "write and the coach's undo took Session/Plan in opposite orders "
            f"again): {athlete_result.get('error')!r}"
        )
        assert "error" not in undo_result, (
            "the coach's undo raised instead of answering — a `deadlock "
            f"detected` here means #562 has regressed: {undo_result.get('error')!r}"
        )
        assert athlete_result.get("status_code") == 200, (
            f"the athlete's line save did not come back 200: "
            f"{athlete_result.get('content')}"
        )
        assert undo_result.get("status_code") == 200, (
            f"the coach's undo did not come back 200: {undo_result.get('content')}"
        )

        # Not vacuous: both sides' writes actually happened.
        s.squat.refresh_from_db()
        assert s.squat.text == original_squat_text, (
            "the coach's undo did not actually restore the squat's line-0 "
            f"text; got {s.squat.text!r}, wanted the pre-edit text "
            f"{original_squat_text!r}"
        )
        athlete_cell = sub_cell(s.squat, 1)
        assert athlete_cell.text == "225 x 5", (
            "the athlete's sub-line write did not actually persist through "
            f"the race; got {athlete_cell.text!r}"
        )
        assert athlete_cell.athlete_authored is True, (
            "the athlete's cell lost its athlete_authored flag — the purge "
            "would no longer be obligated to spare it on a future undo"
        )
        assert not PlanAction.objects.filter(
            plan=s.plan, stack=PlanAction.Stack.UNDO
        ).exists(), "the undo action should have moved off the undo stack"
        assert PlanAction.objects.filter(
            plan=s.plan, stack=PlanAction.Stack.REDO
        ).exists(), "the coach's undo should have pushed a mirror-image redo action"


class TestLockOrderIsPlanThenSession:
    """Pins the ORDER itself, cheaply, so a future swap-back fails here directly.

    The race test above proves the CONSEQUENCE (no deadlock) under one forced
    interleaving; it can't prove a reordering is impossible in general, since
    a deadlock needs real concurrency to manifest on any given run. This test
    instead inspects the SQL a single, ordinary, single-threaded athlete line
    save actually issues, and asserts the ``meso_plan`` lock is acquired
    before the ``meso_session`` one — no threads, no timing, nothing to be
    flaky about.
    """

    def test_athlete_cell_write_locks_the_plan_row_before_the_session_row(self, client):
        s = seed()
        client.force_login(s.athlete)
        with CaptureQueriesContext(connection) as ctx:
            resp = write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert resp.status_code == 200, resp.content

        all_sql = [q["sql"] for q in ctx.captured_queries]
        plan_lock_index = next(
            (
                i
                for i, sql in enumerate(all_sql)
                if "meso_plan" in sql and "FOR NO KEY UPDATE" in sql
            ),
            None,
        )
        session_lock_index = next(
            (
                i
                for i, sql in enumerate(all_sql)
                if "meso_session" in sql and "FOR UPDATE" in sql
            ),
            None,
        )

        assert plan_lock_index is not None, (
            "no `meso_plan ... FOR NO KEY UPDATE` query was issued at all — "
            f"captured SQL: {all_sql}"
        )
        assert session_lock_index is not None, (
            "no `meso_session ... FOR UPDATE` query was issued at all — "
            f"captured SQL: {all_sql}"
        )
        assert plan_lock_index < session_lock_index, (
            "the Plan lock was issued AFTER the Session lock — #562 has "
            f"regressed: plan lock at query #{plan_lock_index}, session lock "
            f"at query #{session_lock_index}; captured SQL: {all_sql}"
        )

        # Not vacuous: the write this SQL belongs to actually happened.
        assert sub_cell(s.squat, 1).text == "225 x 5"
