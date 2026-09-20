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
the SAME row locks the cascade will eventually need, FOUR levels top-down —
``User``, then ``CoachAthlete``, then ``Plan``, then ``AgentProposalBatch``,
each ascending by pk — up front, inside the same transaction as the
``.delete()`` that follows. That doesn't narrow the race window, it removes
the cycle: whichever edit request arrives after ``lock_cascade_parents`` has
run simply waits on a lock the delete already holds, and once the delete
commits, the edit re-reads and answers cleanly (404/409 for a row that's now
gone) instead of racing the delete for the same rows in opposite orders.

**Why the top two levels, ``User`` and ``CoachAthlete``, are there at all.**
Locking just ``Plan``/``AgentProposalBatch`` closes the CHILD-then-parent
cycle above, but opens a second, narrower one: each lock query is one
statement's snapshot, and under READ COMMITTED the delete's own later
collector SELECTs take fresh ones. So a ``Plan`` row INSERTed and committed
*after* the ``Plan``-locking query has already run is still collected by the
cascade — nothing was ever holding its row lock — and the cascade then
deletes that freshly-inserted plan's children before the plan itself: the
same #559 shape, reopened through a door the ``Plan``/batch locks alone
cannot close, because the row they'd need to see does not exist yet when
they run. It's reachable, not hypothetical: ``views.plan_create`` locks only
the ``CoachAthlete`` link before inserting a ``Plan``, so a coach who clicks
"Remove demo data" in one tab and creates a program for a plan-less demo
athlete in another can land in exactly that window. Locking the ``User``/
``CoachAthlete`` rows FIRST — before the cascade's own read of which plans
exist — closes it: ``plan_create`` then blocks on the link lock the delete
already holds, and once the delete commits it finds the link gone and
answers a clean 404, never a race. ``TestClearDemoRacesAConcurrentPlanCreate``
below proves this one the same way as the headline scenario above.

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
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
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
# The second headline scenario: a plan inserted mid-delete escapes the lock.
# ---------------------------------------------------------------------------


class TestClearDemoRacesAConcurrentPlanCreate:
    """The gap the ``User``/``CoachAthlete`` levels close (#558, #562).

    ``views.plan_create`` locks only the ``CoachAthlete`` link before
    inserting a ``Plan`` — ordinary, correct behavior on its own. But
    ``lock_cascade_parents``'s ``Plan`` query is one statement's snapshot:
    under READ COMMITTED, a ``Plan`` row committed AFTER that snapshot is
    still collected by the cascade's own later SELECTs, with nothing ever
    having locked its row — so the cascade would delete that plan's freshly
    inserted children before the plan itself, reopening the #559 cycle
    through a door the ``Plan``/``AgentProposalBatch`` locks alone cannot
    close (see the module docstring). Locking the ``CoachAthlete`` link FIRST
    closes it: the concurrent ``plan_create`` then blocks on the very link
    row ``clear_demo`` locked before it ever queried ``Plan``, and once the
    delete commits it finds the link gone and answers a clean 404 instead of
    inserting anything.

    A demo athlete with no plan yet stands in for the reachable shape —
    ``load_demo`` only builds a full plan tree for "maya" via
    ``load_program``/``_ensure_demo_plan`` (confirmed below, not assumed), so
    any of the other four demo athletes is plan-less by construction: exactly
    the athlete a coach could click "+ New program" for while "Remove demo
    data" is in flight in another tab. Proven the same way as the first
    headline test above: a genuine ``pg_stat_activity`` block, never a sleep
    — but the pause hook here wraps ``demo.lock_cascade_parents`` itself
    (monkeypatched, calling through to the original before pausing) rather
    than a ``post_delete`` signal, since the window this test needs sits
    between two ordinary function calls in ``clear_demo``'s body — AFTER
    ``lock_cascade_parents`` returns, BEFORE the ``.delete()`` that follows —
    not inside the collector's cascade itself.
    """

    def test_a_plan_create_for_a_planless_demo_athlete_blocks_then_404s_not_500(
        self, monkeypatch
    ):
        coach = _coach()
        demo.load_demo(coach)
        # Confirm "devon" is genuinely plan-less rather than assuming it —
        # only "maya" gets a plan tree (``load_program``).
        devon = User.objects.get(email=demo.demo_email(coach, "devon"))
        assert not Plan.objects.filter(relationship__athlete=devon).exists()
        demo_athlete_ids = list(
            CoachAthlete.objects.filter(coach=coach, is_demo=True).values_list(
                "athlete_id", flat=True
            )
        )
        assert devon.pk in demo_athlete_ids

        reached = threading.Event()
        go = threading.Event()
        original_lock_cascade_parents = demo.lock_cascade_parents

        def _paused_lock_cascade_parents(user_ids):
            # Run the REAL locking pass first — every one of the four levels'
            # locks (including the `CoachAthlete` link this test cares about)
            # is genuinely held by the time `reached` fires — then pause
            # `clear_demo` in exactly the window the fix closes: after every
            # lock is taken, before the `.delete()` that follows.
            original_lock_cascade_parents(user_ids)
            reached.set()
            assert go.wait(timeout=5), (
                "the main thread never released the paused "
                "lock_cascade_parents — the test would hang forever otherwise"
            )

        monkeypatch.setattr(demo, "lock_cascade_parents", _paused_lock_cascade_parents)

        clear_demo_errors = []

        def run_clear_demo():
            try:
                demo.clear_demo(coach)
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                clear_demo_errors.append(exc)
            finally:
                connection.close()

        plan_client = Client()
        plan_client.force_login(coach)
        plan_result = {}

        def run_plan_create():
            # The real endpoint, not a bare model call — exercises its own
            # `select_for_update` on the `CoachAthlete` link, its Http404,
            # and its own `transaction.atomic()` too.
            try:
                resp = plan_client.post(
                    reverse("meso:plan_create", kwargs={"pk": devon.pk})
                )
                plan_result["status_code"] = resp.status_code
                plan_result["content"] = resp.content
            except Exception as exc:  # pragma: no cover - surfaced via the assert below
                plan_result["error"] = exc
            finally:
                connection.close()

        clearer = threading.Thread(target=run_clear_demo)
        clearer.start()
        assert reached.wait(timeout=5), (
            "clear_demo never reached the paused lock_cascade_parents — "
            "either it never got this far, or the monkeypatch never took"
        )

        creator = threading.Thread(target=run_plan_create)
        creator.start()

        assert _wait_until_a_backend_is_lock_blocked(timeout=5.0), (
            "no backend was ever reported lock-blocked by pg_stat_activity — "
            "plan_create either raced past the CoachAthlete lock instead of "
            "blocking on it, or never reached its own select_for_update at all"
        )
        # Still running, not finished — it found the link lock held by
        # clear_demo and is waiting there, not racing ahead of it.
        assert creator.is_alive(), (
            "plan_create finished before being released — it never actually "
            "blocked on clear_demo's CoachAthlete lock"
        )

        go.set()
        clearer.join(timeout=10)
        creator.join(timeout=10)

        assert not clearer.is_alive(), "clear_demo's thread never finished"
        assert not creator.is_alive(), "the plan_create thread never finished"

        assert clear_demo_errors == [], (
            f"clear_demo raised (deadlock?): {clear_demo_errors}"
        )
        assert "error" not in plan_result, (
            "plan_create raised instead of answering (deadlock?): "
            f"{plan_result.get('error')}"
        )
        for exc in clear_demo_errors + (
            [plan_result["error"]] if "error" in plan_result else []
        ):
            assert "deadlock" not in str(exc).lower(), (
                f"PostgreSQL reported a deadlock: {exc}"
            )

        # `plan_create` re-reads the link under its own lock; by the time it
        # wakes up, clear_demo has committed and the link is gone — a clean
        # 404 (`Http404("Unknown athlete")`), never a 500, and never a plan.
        assert plan_result.get("status_code") != 500, (
            f"plan_create came back 500: {plan_result.get('content')}"
        )
        assert plan_result.get("status_code") == 404, (
            "expected plan_create's Http404 for a link clear_demo had already "
            f"removed, got {plan_result.get('status_code')}: "
            f"{plan_result.get('content')}"
        )

        # No demo athlete escaped, and the race didn't leave a Plan (or any
        # Mesocycle/Prescription orphaned under one) for the athlete
        # `plan_create` raced to create it for.
        assert demo.has_demo(coach) is False
        assert not User.objects.filter(pk__in=demo_athlete_ids).exists()
        assert not CoachAthlete.objects.filter(coach=coach).exists()
        assert not Plan.objects.filter(relationship__coach=coach).exists()
        assert not Mesocycle.objects.filter(plan__relationship__coach=coach).exists()
        assert not Prescription.objects.filter(
            week__mesocycle__plan__relationship__coach=coach
        ).exists()


def _first_query_index(queries, predicate):
    """The index of the first captured SQL statement matching ``predicate``.

    ``predicate`` receives the already-lowercased SQL. Raises with the full
    query list on no match, rather than returning a sentinel, so a failure
    shows exactly what WAS captured.
    """
    for i, sql in enumerate(queries):
        if predicate(sql.lower()):
            return i
    raise AssertionError("no matching query among:\n" + "\n".join(queries))


def _lock_cascade_indices(queries):
    """First-index position of each ``lock_cascade_parents`` level's lock query.

    Matches on the table's own quoted, delimited name — ``from "meso_plan"``
    — never a bare substring. ``"meso_plan"`` (no trailing quote) is ALSO a
    substring of ``"meso_planaction"``, the unrelated ``PlanAction`` model's
    table, so a looser ``"meso_plan" in low`` check could be satisfied by a
    ``PlanAction`` query instead of the ``Plan`` lock it's meant to find. The
    same discipline is applied to every level for consistency, though only
    ``meso_plan`` has a same-prefixed sibling table today.
    """
    return {
        "user": _first_query_index(
            queries,
            # #590 adds a single-row coach mutex before the cascade. The
            # cascade's demo-athlete User query is the ordered one.
            lambda low: (
                'from "users_user"' in low
                and "for no key update" in low
                and "order by" in low
            ),
        ),
        "coachathlete": _first_query_index(
            queries,
            lambda low: (
                'from "meso_coachathlete"' in low and "for no key update" in low
            ),
        ),
        "plan": _first_query_index(
            queries,
            lambda low: 'from "meso_plan"' in low and "for no key update" in low,
        ),
        "batch": _first_query_index(
            queries,
            lambda low: (
                'from "meso_agentproposalbatch"' in low and "for no key update" in low
            ),
        ),
        "delete": _first_query_index(
            queries, lambda low: low.strip().startswith("delete")
        ),
    }


# ---------------------------------------------------------------------------
# Cheaper test 1 — the lock order + ascending-pk claim, pinned via SQL.
# ---------------------------------------------------------------------------


class TestClearDemoLockOrder:
    """After its coach mutex, clear locks all cascade levels before deleting.

    ``User``, then ``CoachAthlete``, then ``Plan``, then ``AgentProposalBatch``
    — each query ascending by pk (#558, #559, #562). This doesn't force any
    interleaving; it just captures the SQL ``clear_demo`` actually issues and
    asserts the sequence and shape the fix promises. It survives even if the
    headline races above can never be reliably forced on a given
    machine/CI runner. Pinning all four (not just ``Plan``/``AgentProposalBatch``)
    matters because a regression that quietly dropped the top two levels —
    reopening the plan-created-mid-delete window
    ``TestClearDemoRacesAConcurrentPlanCreate`` covers — would otherwise still
    pass a Plan-then-batch-only check.
    """

    def test_locks_users_then_links_then_plans_then_batches_ascending_before_any_delete(
        self,
    ):
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
        idx = _lock_cascade_indices(queries)

        assert (idx["user"] < idx["coachathlete"] < idx["plan"] < idx["batch"]) and idx[
            "batch"
        ] < idx["delete"], (
            "expected User locked, then CoachAthlete, then Plan, then "
            f"AgentProposalBatch, then the first DELETE (got {idx}):\n"
            + "\n".join(queries)
        )

        # The rework dropped the join that used to make `of=("self",)`
        # necessary on the Plan query — every filter is on a local column
        # now. `Plan.relationship` is nullable, so a joined form promotes to
        # a LEFT OUTER JOIN, and a bare `FOR UPDATE` over one of those is a
        # hard PostgreSQL error, not a silent over-lock — so a regression
        # back to a joined query wouldn't quietly over-lock, it would crash
        # `clear_demo` outright. Pin the emitted clause itself: never
        # `FOR UPDATE OF`.
        plan_sql = queries[idx["plan"]].lower()
        assert "for update of" not in plan_sql, plan_sql

        # And pin the STRENGTH, on every level. `FOR NO KEY UPDATE`, not
        # `FOR UPDATE`, is what keeps these locks from conflicting with the
        # commit-time `FOR KEY SHARE` a deferred FK check takes on a parent
        # row — and holding `FOR UPDATE` across a later lock acquisition here
        # is a deadlock generator, not merely a wider lock (see
        # `lock_cascade_parents`' docstring for the cycle it produced against
        # a concurrent `demo_load` segment). A silent regression to plain
        # `FOR UPDATE` reintroduces that, so assert the emitted clause rather
        # than trusting the call site.
        for level in ("user", "coachathlete", "plan", "batch"):
            sql = queries[idx[level]]
            low = sql.lower()
            assert "for no key update" in low, sql

        # Ascending by pk, proven at the SQL level: `lock_cascade_parents`
        # relies on Postgres's LockRows node sitting ABOVE the sort (its own
        # docstring), so an explicit, non-descending ORDER BY is what actually
        # guarantees ascending acquisition order — inspecting which rows got
        # locked in which order isn't otherwise observable from outside the
        # transaction. Each query here selects only the pk column (``.values_list
        # ("pk", flat=True)``), so Django's compiler may emit a positional
        # ``ORDER BY 1 ASC`` rather than naming the column — either form pins
        # the claim equally well, so this checks for "ascending" (``asc``
        # present, ``desc`` absent) rather than a literal column name.
        for level in ("user", "coachathlete", "plan", "batch"):
            sql = queries[idx[level]]
            low = sql.lower()
            assert "order by" in low, sql
            order_clause = low.split("order by", 1)[1].split("for no key update")[0]
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
    """``expire_sandboxes`` locks the sandbox coach's own four-level tree too.

    The same way ``clear_demo`` locks a demo athlete's (#558, #559, #562),
    before deleting the coach. The reaping behavior itself (including the
    demo-athlete leak trap) is ``test_sandbox.py``'s job; this only pins the
    locking half plus a smoke check that the sweep still actually reaps.

    The sandbox coach here has NO demo athletes at all, so `expire_sandboxes`'s
    own `demo.clear_demo(session.user)` call is a no-op (`_demo_athletes`
    returns nothing) and issues no lock/delete queries of its own — every
    lock query captured here comes from the `lock_cascade_parents([session.
    user_id])` call around the coach's own delete, which is the half this
    test exists to pin. The `User` and `CoachAthlete` lock queries still run
    and are still captured even though this coach has no links at all — the
    `CoachAthlete` query matches zero rows, not zero statements. The plan is
    a TEMPLATE (`owner=user`, no `relationship`) for the same reason: it's
    reachable only through the coach's own cascade, not through any
    `is_demo` athlete link.
    """

    def test_locks_users_then_links_then_plan_then_batch_before_deleting_the_sandbox_coach(
        self,
    ):
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
        idx = _lock_cascade_indices(queries)

        assert (idx["user"] < idx["coachathlete"] < idx["plan"] < idx["batch"]) and idx[
            "batch"
        ] < idx["delete"], (
            "expected User locked, then CoachAthlete, then Plan, then "
            f"AgentProposalBatch, then the first DELETE (got {idx}):\n"
            + "\n".join(queries)
        )

        # Smoke check: the sweep didn't just lock correctly, it still reaped.
        assert not User.objects.filter(pk=user.pk).exists()
        assert not Plan.objects.filter(pk=template_plan.pk).exists()
        assert not AgentProposalBatch.objects.filter(pk=batch.pk).exists()
