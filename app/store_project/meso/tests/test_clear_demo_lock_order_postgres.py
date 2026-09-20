r"""PostgreSQL-only tests for the cascade-delete/edit lock cycle (#559).

Every edit path in this app locks a PARENT row and only then writes its
CHILDREN: ``batch_apply``/``change_set_status`` lock the ``AgentProposalBatch``
and then write its ``ProposedChange`` rows (#540); designer edits lock the
``Plan`` and then write ``Prescription``/``Session`` rows. Django's
``Collector.delete`` — what runs underneath a plain ``queryset.delete()`` or
``some_user.delete()`` — walks the tree the OTHER way round: it fast-deletes
the leaves first (``DELETE FROM meso_proposedchange WHERE batch_id IN (...)``,
no row-by-row locking, no signals), then runs any ``SET_NULL``/``SET_DEFAULT``
field updates, and only at the very end deletes the parents it collected
along the way. A cascade delete of a demo athlete or a sandbox coach is
therefore CHILD-then-parent, exactly backwards from every edit path's
parent-then-child, and the two overlapping is a lock cycle: whichever side
Postgres picks loses with ``deadlock detected`` — a 500, not a 409/404.

The two user-facing delete paths that reach a ``Plan``/``AgentProposalBatch``
tree are ``demo.clear_demo`` ("Remove demo data") and the sandbox expiry sweep
(``sandbox.expire_sandboxes``). The fix, ``demo.lock_cascade_parents``, takes
the SAME row locks the cascade will eventually need — the ``Plan`` rows, then
their ``AgentProposalBatch`` rows, both ascending by pk — up front, inside the
same transaction as the ``.delete()`` that follows. That doesn't narrow the
race window, it removes the cycle: whichever edit request arrives after
``lock_cascade_parents`` has run simply waits on a lock the delete already
holds, and once the delete commits, the edit re-reads and answers cleanly
(404/409 for a row that's now gone) instead of racing the delete for the same
two rows in opposite orders.

**Why this is only expressible on PostgreSQL.** ``select_for_update`` is a
documented no-op on SQLite, and the default in-memory SQLite test database
doesn't even share rows across threads/connections — each thread gets its own
private database, so two threads can never contend for the same row lock at
all, let alone deadlock over it. A missing lock and a working one look
identical there. Same blind spot as ``test_undo_purge_postgres.py``,
``test_settle_postgres.py``, and ``test_billing_webhook_postgres.py``.

**How the delete is paused mid-cascade, and why that's a faithful stand-in.**
The issue needs the delete to pause in the narrow window BETWEEN its child
fast-deletes and its parent updates/deletes, so a concurrent edit can be made
to land there deterministically instead of by luck. There is no supported
hook in that window as Django ships it — the child rows vanish via one raw
``DELETE ... WHERE batch_id IN (...)`` with no signals and nothing to attach
to. So each test that needs the pause registers a ``post_delete`` receiver on
``ProposedChange`` for its own duration
(``_paused_on_proposed_change_delete``). ``Collector.can_fast_delete`` refuses
to fast-delete any model with a ``pre_delete``/``post_delete`` receiver
attached (``_has_signal_listeners``), so registering one is enough to flip
``ProposedChange`` from "one fast batched ``DELETE``, no hook, no signals" to
"a per-instance delete that sends ``post_delete`` for each row it removes" —
a real, if slower, code path Django itself falls back to for any model that
has a receiver, not a special test-only mode. This is a faithful stand-in for
the shape of the bug precisely because it changes ONLY the mechanics of
deleting the child (batched-and-silent vs. per-instance-and-signalled); it
does not touch, and could not touch, the essential shape at the center of
#559 — child rows are still gone from the table before the parent
(``AgentProposalBatch``, and above it ``Plan``) is touched at all. The
receiver fires exactly once the child ``DELETE`` has executed and before the
collector goes anywhere near the batch, which is exactly the window the issue
describes. It is disconnected in a ``try``/``finally`` no matter how the test
exits, so it never leaks into an unrelated test in the same process.

**How the block is proven, not assumed.** Thread A (``demo.clear_demo`` or,
for the cheaper tests, ``demo.lock_cascade_parents`` directly) signals a
``threading.Event`` the instant it is paused inside the receiver, holding
whatever locks the fix says it should already hold at that point. Thread B
(the real ``change_set_status`` endpoint, driven through the Django test
client, not called as a bare function) then attempts the same batch lock.
Rather than inferring "B must be blocked by now" from a fixed sleep — flaky
in both directions on a loaded CI runner — the main thread polls PostgreSQL's
own ``pg_stat_activity`` (``wait_event_type = 'Lock'``) from a THIRD
connection until it reports some backend genuinely waiting on a lock, with a
timeout so a wedged suite fails loudly instead of hanging. Only then is
thread A released. This is the same technique ``test_undo_purge_postgres.py``
uses; ``_wait_until_a_backend_is_lock_blocked`` below is copied from there
rather than imported, matching this file's siblings (each Postgres-only test
module here is self-contained).

**What "without the fix" would look like here, and why the test would catch
it.** Undo ``lock_cascade_parents`` in your head and replay the headline test:
``clear_demo`` would fast-delete the ``ProposedChange`` row (still visible
to this file's paused receiver, since fast-delete is disabled for the
*test's* purposes regardless) while holding no lock on the batch at all. The
concurrent approve would then win the batch's ``select_for_update``
immediately — it isn't contending with anything — pass its re-check (the
batch is still ``PENDING``), and try to ``UPDATE`` the very
``ProposedChange`` row ``clear_demo`` already deleted (but not yet
committed). That ``UPDATE`` blocks on the row lock A's delete implicitly
holds. Releasing A then makes A try to delete the parent
``AgentProposalBatch`` row B is holding under its own ``select_for_update``
— the cycle closes, and Postgres kills one side with ``deadlock detected``.
The headline test asserts neither thread raised, and explicitly asserts no
exception anywhere mentions "deadlock" — so it would fail loudly in that
world instead of passing by accident.

Run locally against a dedicated Postgres database (never the shared dev one —
this file, like its siblings, creates and destroys real rows via Django's
test-DB machinery)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/lock559 \
        uv run pytest app/store_project/meso/tests/test_clear_demo_lock_order_postgres.py -v
"""

import contextlib
import json
import threading
import time

import pytest
from django.db import connection
from django.db import transaction
from django.db.models.signals import post_delete
from django.db.utils import OperationalError
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.meso.models import SandboxSession
from store_project.users.factories import UserFactory
from store_project.users.models import User

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


def _coach():
    coach = UserFactory()
    CoachProfileFactory(user=coach)
    return coach


def _demo_athlete_link_and_plan(coach, *, status=CoachAthlete.Status.ACTIVE):
    """One demo athlete + an ``is_demo`` link + an ACTIVE plan for ``coach``.

    Deliberately NOT ``demo.load_demo`` — that builds five athletes and a full
    mesocycle/week/session tree, which is what ``clear_demo`` cares about
    scoping correctly (``test_demo.py`` covers that already) but is far more
    than these lock-order tests need. ``clear_demo`` only requires the link to
    carry ``is_demo=True``; a bare plan with no mesocycle tree still gives
    ``lock_cascade_parents`` a real ``Plan`` row to find and lock.
    """
    athlete = UserFactory()
    link = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=status, is_demo=True
    )
    plan = PlanFactory(relationship=link, status=Plan.Status.ACTIVE)
    return athlete, link, plan


def _demo_athlete_plan_and_batch(coach):
    """The above, plus a PENDING batch on the plan (no ``ProposedChange`` yet)."""
    athlete, link, plan = _demo_athlete_link_and_plan(coach)
    batch = AgentProposalBatchFactory(plan=plan)
    return athlete, link, plan, batch


def _wait_until_a_backend_is_lock_blocked(timeout=5.0, interval=0.02):
    """Poll ``pg_stat_activity`` until some backend is genuinely waiting on a lock.

    Uses the calling (main) thread's own connection — a THIRD connection,
    distinct from both worker threads' — so this is Postgres itself
    confirming the block, not a guess based on elapsed time. Returns
    ``True`` once seen, ``False`` on timeout. Copied from
    ``test_undo_purge_postgres.py`` (kept file-local like its other siblings).
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


@contextlib.contextmanager
def _paused_on_proposed_change_delete(target_batch_id):
    """Pause a cascade delete right after it deletes ``target_batch_id``'s changes.

    Registers a ``post_delete`` receiver on ``ProposedChange`` for the
    duration of the ``with`` block — see the module docstring for why that
    (and only that) is enough to turn the child model's delete from one silent
    fast ``DELETE`` into a per-instance delete-then-signal the collector runs
    strictly BEFORE it ever touches the parent ``AgentProposalBatch`` row.

    Yields ``(reached, go)``: the receiver sets ``reached`` the moment it
    fires and then blocks on ``go`` (with a timeout, so a bug that never
    releases it fails the test instead of hanging the suite forever) before
    letting the delete continue. Always disconnects the receiver on the way
    out, success or failure, so it can never leak into another test.
    """
    reached = threading.Event()
    go = threading.Event()

    def _pause(sender, instance, **kwargs):
        if instance.batch_id != target_batch_id:
            return
        reached.set()
        assert go.wait(timeout=5), (
            "the main thread never released the paused post_delete — the "
            "test would hang forever otherwise"
        )

    dispatch_uid = f"test-559-pause-{target_batch_id}"
    post_delete.connect(
        _pause, sender=ProposedChange, dispatch_uid=dispatch_uid, weak=False
    )
    try:
        yield reached, go
    finally:
        post_delete.disconnect(_pause, sender=ProposedChange, dispatch_uid=dispatch_uid)


# ---------------------------------------------------------------------------
# The headline scenario: "Remove demo data" racing a coach's approve.
# ---------------------------------------------------------------------------


class TestClearDemoRacesAConcurrentApprove:
    """The exact shape #559 describes.

    A coach clicks "Remove demo data" in one tab and approves a proposed
    change on the same demo athlete's batch in another, at the moment the
    delete is between its child fast-delete and its parent updates. With the
    fix, both requests finish and neither is a 500.
    """

    def test_both_finish_clean_no_deadlock_and_demo_data_is_gone(self):
        coach = _coach()
        athlete, link, plan = _demo_athlete_link_and_plan(coach)
        batch = AgentProposalBatchFactory(plan=plan)
        change = ProposedChangeFactory(batch=batch)

        clear_demo_errors = []

        def run_clear_demo():
            # Thread A: the delete. `lock_cascade_parents` (inside
            # `clear_demo`'s own `transaction.atomic()`) has already taken the
            # Plan and batch locks by the time the collector reaches this
            # batch's `ProposedChange` rows — the pause below fires strictly
            # after that.
            try:
                demo.clear_demo(coach)
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                clear_demo_errors.append(exc)
            finally:
                connection.close()

        approve_client = Client()
        approve_client.force_login(coach)
        approve_result = {}

        def run_the_approve():
            # Thread B: the real endpoint, not a bare call into
            # `change_set_status` — this exercises its own pre-check, JSON
            # parsing, and `transaction.atomic()` too.
            try:
                resp = approve_client.post(
                    reverse("meso:api_change_status", kwargs={"pk": change.pk}),
                    data=json.dumps({"status": "approved"}),
                    content_type="application/json",
                )
                approve_result["status_code"] = resp.status_code
                approve_result["content"] = resp.content
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                approve_result["error"] = exc
            finally:
                connection.close()

        with _paused_on_proposed_change_delete(batch.pk) as (reached, go):
            clearer = threading.Thread(target=run_clear_demo)
            clearer.start()
            assert reached.wait(timeout=5), (
                "clear_demo never reached the paused post_delete of the "
                "batch's ProposedChange — either the receiver never fired "
                "(fast-delete still in play for this model) or the delete "
                "never got this far"
            )

            approver = threading.Thread(target=run_the_approve)
            approver.start()

            assert _wait_until_a_backend_is_lock_blocked(timeout=5.0), (
                "no backend was ever reported lock-blocked by pg_stat_activity "
                "— the approve either raced past the batch lock instead of "
                "blocking on it, or never reached its own select_for_update "
                "at all"
            )
            # Still running, not finished — it found the batch lock held by
            # clear_demo and is waiting there, not racing ahead of it.
            assert approver.is_alive(), (
                "the approve thread finished before being released — it "
                "never actually blocked on clear_demo's batch lock"
            )

            go.set()
            clearer.join(timeout=10)
            approver.join(timeout=10)

        assert not clearer.is_alive(), "clear_demo's thread never finished"
        assert not approver.is_alive(), "the approve thread never finished"

        assert clear_demo_errors == [], (
            f"clear_demo raised (deadlock?): {clear_demo_errors}"
        )
        assert "error" not in approve_result, (
            "the approve request raised instead of answering (deadlock?): "
            f"{approve_result.get('error')}"
        )
        for exc in clear_demo_errors + (
            [approve_result["error"]] if "error" in approve_result else []
        ):
            assert "deadlock" not in str(exc).lower(), (
                f"PostgreSQL reported a deadlock: {exc}"
            )

        # `change_set_status` re-reads the batch under its own lock; by the
        # time it wakes up, clear_demo has committed and the batch is gone —
        # a clean 404, never a 500.
        assert approve_result.get("status_code") != 500, (
            f"the approve came back 500: {approve_result.get('content')}"
        )
        assert approve_result.get("status_code") == 404, (
            "expected change_set_status's Http404 for a batch clear_demo had "
            f"already removed, got {approve_result.get('status_code')}: "
            f"{approve_result.get('content')}"
        )

        assert demo.has_demo(coach) is False
        assert not User.objects.filter(pk=athlete.pk).exists()
        assert not CoachAthlete.objects.filter(pk=link.pk).exists()
        assert not Plan.objects.filter(pk=plan.pk).exists()
        assert not AgentProposalBatch.objects.filter(pk=batch.pk).exists()
        assert not ProposedChange.objects.filter(pk=change.pk).exists()


# ---------------------------------------------------------------------------
# Cheaper test 1 — the lock order + ascending-pk claim, pinned via SQL.
# ---------------------------------------------------------------------------


class TestClearDemoLockOrder:
    """``clear_demo`` must lock Plans, then batches, then delete — never the reverse.

    Each query ascending by pk (#559). This doesn't force any interleaving; it just captures the SQL
    ``clear_demo`` actually issues and asserts the sequence and shape the fix
    promises. It survives even if the headline race above can never be
    reliably forced on a given machine/CI runner.
    """

    def test_locks_plans_then_batches_ascending_before_any_delete(self):
        coach = _coach()
        # Two of each, so "ascending by pk" has more than one row to actually
        # order — a single-row query would trivially satisfy any order.
        _, _, plan_a, batch_a = _demo_athlete_plan_and_batch(coach)
        _, _, plan_b, batch_b = _demo_athlete_plan_and_batch(coach)
        assert plan_a.pk < plan_b.pk
        assert batch_a.pk < batch_b.pk

        with CaptureQueriesContext(connection) as ctx:
            demo.clear_demo(coach)

        queries = [q["sql"] for q in ctx.captured_queries]

        def first_index(predicate):
            for i, sql in enumerate(queries):
                if predicate(sql.lower()):
                    return i
            raise AssertionError("no matching query among:\n" + "\n".join(queries))

        plan_lock_idx = first_index(
            lambda low: "meso_plan" in low and "for update" in low
        )
        batch_lock_idx = first_index(
            lambda low: "meso_agentproposalbatch" in low and "for update" in low
        )
        first_delete_idx = first_index(lambda low: low.strip().startswith("delete"))

        assert plan_lock_idx < batch_lock_idx < first_delete_idx, (
            "expected Plan locked, then AgentProposalBatch locked, then the "
            f"first DELETE (got indices {plan_lock_idx}, {batch_lock_idx}, "
            f"{first_delete_idx}):\n" + "\n".join(queries)
        )

        # Ascending by pk, proven at the SQL level: `lock_cascade_parents`
        # relies on Postgres's LockRows node sitting ABOVE the sort (its own
        # docstring), so an explicit, non-descending ORDER BY is what actually
        # guarantees ascending acquisition order — inspecting which rows got
        # locked in which order isn't otherwise observable from outside the
        # transaction. Each query here selects only the pk column (``.values_list
        # ("pk", flat=True)``), so Django's compiler emits a positional
        # ``ORDER BY 1 ASC`` rather than naming the column — either form pins
        # the claim equally well, so this checks for "ascending" (``asc``
        # present, ``desc`` absent) rather than a literal column name.
        for sql in (queries[plan_lock_idx], queries[batch_lock_idx]):
            low = sql.lower()
            assert "order by" in low, sql
            order_clause = low.split("order by", 1)[1].split("for update")[0]
            assert "desc" not in order_clause, sql
            assert "asc" in order_clause, sql


# ---------------------------------------------------------------------------
# Cheaper test 2 — lock_cascade_parents reaches exactly the right rows.
# ---------------------------------------------------------------------------


class TestLockCascadeParentsScope:
    """``lock_cascade_parents`` must lock only the demo athlete's own Plan/batch.

    Nothing that belongs to a different athlete or a different coach (#559).
    ``clear_demo`` never touching real/other-coach data predates this fix, but
    the new locking query is a NEW way to get that wrong: an over-broad
    ``Q(...)`` here wouldn't corrupt data (it only locks), but it would create
    exactly the kind of surprise contention/deadlock exposure this whole fix
    exists to remove.

    Proven the same way as any lock: hold ``lock_cascade_parents``'s
    transaction open on one thread, then try a ``NOWAIT`` lock on each
    candidate row from a second connection. ``NOWAIT`` raises immediately if
    the row is locked and returns immediately if it isn't — no polling
    needed, unlike the headline test's genuine blocking wait.
    """

    def test_locks_the_demo_plan_and_batch_not_other_coachs_or_real_data(self):
        coach = _coach()
        demo_athlete, demo_link, demo_plan = _demo_athlete_link_and_plan(coach)
        demo_batch = AgentProposalBatchFactory(plan=demo_plan)

        # Same coach, a REAL (non-demo) athlete — must survive a call scoped
        # to just the demo athlete's pk.
        real_athlete = UserFactory()
        real_link = CoachAthleteFactory(
            coach=coach,
            athlete=real_athlete,
            status=CoachAthlete.Status.ACTIVE,
            is_demo=False,
        )
        real_plan = PlanFactory(relationship=real_link, status=Plan.Status.ACTIVE)
        real_batch = AgentProposalBatchFactory(plan=real_plan)

        # A completely different coach's own demo — unrelated to the coach
        # above; nothing about their pk should be reachable from it.
        other_coach = _coach()
        _, _, other_plan = _demo_athlete_link_and_plan(other_coach)
        other_batch = AgentProposalBatchFactory(plan=other_plan)

        holding = threading.Event()
        release = threading.Event()

        def hold_the_lock():
            try:
                with transaction.atomic():
                    demo.lock_cascade_parents([demo_athlete.pk])
                    holding.set()
                    assert release.wait(timeout=5), (
                        "the main thread never released the held lock"
                    )
            finally:
                connection.close()

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        assert holding.wait(timeout=5), "lock_cascade_parents never acquired its locks"

        def locked(queryset):
            """True if some row `queryset` resolves to is held by another txn."""
            try:
                with transaction.atomic():
                    list(queryset.select_for_update(nowait=True))
                return False
            except OperationalError as exc:
                assert "could not obtain lock" in str(exc).lower(), exc
                return True

        try:
            assert locked(Plan.objects.filter(pk=demo_plan.pk)) is True
            assert locked(AgentProposalBatch.objects.filter(pk=demo_batch.pk)) is True
            assert locked(Plan.objects.filter(pk=real_plan.pk)) is False
            assert locked(AgentProposalBatch.objects.filter(pk=real_batch.pk)) is False
            assert locked(Plan.objects.filter(pk=other_plan.pk)) is False
            assert locked(AgentProposalBatch.objects.filter(pk=other_batch.pk)) is False
        finally:
            release.set()
            holder.join(timeout=5)
        assert not holder.is_alive(), "the holder thread never finished"


# ---------------------------------------------------------------------------
# Cheaper test 3 — sandbox.expire_sandboxes takes the same locks.
# ---------------------------------------------------------------------------


class TestExpireSandboxesLockOrder:
    """``expire_sandboxes`` locks the sandbox coach's own Plan/batch tree too.

    The same way ``clear_demo`` locks a demo athlete's (#559), before deleting
    the coach. The reaping behavior itself (including the demo-athlete leak
    trap) is ``test_sandbox.py``'s job; this only pins the locking half plus a
    smoke check that the sweep still actually reaps.

    The sandbox coach here has NO demo athletes at all, so `expire_sandboxes`'s
    own `demo.clear_demo(session.user)` call is a no-op (`_demo_athletes`
    returns nothing) and issues no lock/delete queries of its own — the only
    Plan/AgentProposalBatch lock queries captured come from the
    `lock_cascade_parents([session.user_id])` call around the coach's own
    delete, which is the half this test exists to pin. The plan is a
    TEMPLATE (`owner=user`, no `relationship`) for the same reason: it's
    reachable only through the coach's own cascade, not through any
    `is_demo` athlete link.
    """

    def test_locks_plan_then_batch_before_deleting_the_sandbox_coach(self):
        user = sandbox.create_sandbox()
        template_plan = PlanFactory(
            relationship=None,
            is_template=True,
            owner=user,
            status=Plan.Status.ACTIVE,
        )
        batch = AgentProposalBatchFactory(plan=template_plan, coach=user)
        SandboxSession.objects.filter(user=user).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1)
        )

        with CaptureQueriesContext(connection) as ctx:
            reaped = sandbox.expire_sandboxes()

        assert reaped == 1

        queries = [q["sql"] for q in ctx.captured_queries]

        def first_index(predicate):
            for i, sql in enumerate(queries):
                if predicate(sql.lower()):
                    return i
            raise AssertionError("no matching query among:\n" + "\n".join(queries))

        plan_lock_idx = first_index(
            lambda low: "meso_plan" in low and "for update" in low
        )
        batch_lock_idx = first_index(
            lambda low: "meso_agentproposalbatch" in low and "for update" in low
        )
        first_delete_idx = first_index(lambda low: low.strip().startswith("delete"))

        assert plan_lock_idx < batch_lock_idx < first_delete_idx, (
            "expected Plan locked, then AgentProposalBatch locked, then the "
            f"first DELETE (got indices {plan_lock_idx}, {batch_lock_idx}, "
            f"{first_delete_idx}):\n" + "\n".join(queries)
        )

        # Smoke check: the sweep didn't just lock correctly, it still reaped.
        assert not User.objects.filter(pk=user.pk).exists()
        assert not Plan.objects.filter(pk=template_plan.pk).exists()
        assert not AgentProposalBatch.objects.filter(pk=batch.pk).exists()
