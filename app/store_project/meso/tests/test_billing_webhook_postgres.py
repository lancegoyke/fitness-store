r"""PostgreSQL-only regression tests for the billing webhook's row lock (#546).

``billing/webhooks.py`` writes ``subscription_started``/``subscription_cancelled``
analytics events guarded by ``_recorded()``, a plain ``Event.objects.filter(...)
.exists()`` check-then-insert. Stripe delivers webhooks **at-least-once** and
**concurrently** (retries, or a created + an invoice.paid landing at once); two
overlapping deliveries of the same event both reading "not recorded yet" both
write the event, and a stale event can clobber a mirror a concurrent delivery
just updated. ``_lock_mirror`` (``select_for_update()`` on the mirror row, or on
the coach's user row before the mirror exists) serializes concurrent deliveries
for the same coach — see ``webhooks.py``'s module docstring for the full
contract.

**Why this file exists separately.** ``select_for_update`` is a documented
no-op on SQLite, and the default in-memory SQLite test database doesn't even
share rows across threads/connections — each thread gets its own private
database, so two threads can never contend for the same row lock at all. A
missing lock and a working one would look identical there: a test for lock
*contention* is simply not expressible on SQLite. Same blind spot as
``test_settle_postgres.py`` and ``analytics/tests/test_track_postgres.py``.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \
        uv run pytest app/store_project/meso/tests/test_billing_webhook_postgres.py -v
"""

import contextlib
import threading
import time
from unittest import mock

import pytest
from django.db import connection

from store_project.analytics.events import EventName
from store_project.meso.billing import webhooks
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachSubscription
from store_project.meso.tests.test_billing_stripe import _coach_with_customer
from store_project.meso.tests.test_billing_stripe import _events
from store_project.meso.tests.test_billing_stripe import _real_sub_event

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


#: The race window: after a delivery has read the mirror, before it writes it.
BEFORE_MIRROR_WRITE = (CoachSubscription.objects, "update_or_create", "before")
#: After a delivery has checked the ledger, before it records the event.
AFTER_LEDGER_CHECK = (webhooks, "_recorded", "after")


def _paused(func, when, reached):
    """``func`` with a pause on a two-party barrier ``"before"``/``"after"`` it runs.

    ``reached`` is set on the first arrival. Whichever thread arrives second
    then sleeps briefly: with no lock, that lets thread A win any lock the rest
    of the path takes incidentally (``update_or_create``'s own
    ``select_for_update``), so the unlocked case fails every run rather than
    most runs. With the lock, B only arrives after A has committed, so the
    sleep changes nothing.
    """
    barrier = threading.Barrier(2, timeout=2)
    arrivals = []
    arrivals_lock = threading.Lock()

    def pause():
        with arrivals_lock:
            arrivals.append(threading.get_ident())
            second = len(arrivals) == 2
        reached.set()
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        if second:
            time.sleep(0.3)

    def wrapper(*args, **kwargs):
        if when == "before":
            pause()
        result = func(*args, **kwargs)
        if when == "after":
            pause()
        return result

    return wrapper


def _race(first_event, second_event, *hooks):
    """Deliver two events at once, the threads meeting at each hook.

    Each hook is ``(owner, attribute, "before" | "after")``. Thread A starts
    first, and B once A has reached a hook. With the lock, B blocks inside
    ``_lock_mirror`` while A waits at the barrier; A's wait times out, A
    commits, and B then reads what A committed. Without it, both threads
    meet at every barrier holding the same stale reads.

    Returns the exceptions either thread raised.
    """
    a_reached = threading.Event()
    errors = []

    def run(event):
        try:
            webhooks.handle_event(event)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    with contextlib.ExitStack() as stack:
        for owner, name, when in hooks:
            wrapper = _paused(getattr(owner, name), when, a_reached)
            stack.enter_context(mock.patch.object(owner, name, wrapper))
        thread_a = threading.Thread(target=run, args=(first_event,))
        thread_a.start()
        assert a_reached.wait(timeout=5), "thread A never reached a hook"
        thread_b = threading.Thread(target=run, args=(second_event,))
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "thread A did not finish"
    assert not thread_b.is_alive(), "thread B did not finish"
    return errors


class TestSameEventTwice:
    """The issue's race: two deliveries of the same ``created`` event."""

    def test_one_started_event_and_an_active_mirror(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="",
            stripe_item_id="",
        )
        event_a = _real_sub_event(
            "customer.subscription.created", sub_id="sub_1", status="active"
        )
        event_b = _real_sub_event(
            "customer.subscription.created", sub_id="sub_1", status="active"
        )

        errors = _race(event_a, event_b, BEFORE_MIRROR_WRITE, AFTER_LEDGER_CHECK)

        assert errors == [], f"worker thread(s) raised: {errors}"
        started = _events(EventName.SUBSCRIPTION_STARTED)
        assert len(started) == 1
        assert started[0].props["subscription"] == "sub_1"
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"


class TestMirrorLockLoadBearing:
    """A stale event delivered concurrently can't clobber a newer takeover."""

    def test_a_new_live_subscription_survives_a_concurrent_stale_update(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_old",
        )
        # Thread A: a fresh re-subscribe, a genuinely new + live subscription.
        event_a = _real_sub_event(
            "customer.subscription.created", sub_id="sub_new", status="active"
        )
        # Thread B: a stale ``updated`` for the *old*, now-superseded subscription.
        event_b = _real_sub_event(
            "customer.subscription.updated", sub_id="sub_old", status="active"
        )

        errors = _race(event_a, event_b, BEFORE_MIRROR_WRITE)

        assert errors == [], f"worker thread(s) raised: {errors}"
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_new"


class TestNoRowFallbackLoadBearing:
    """The user-row fallback lock, for a coach with no mirror row yet."""

    def test_started_and_cancelled_both_recorded_for_a_fresh_coach(self):
        coach = _coach_with_customer()
        assert not CoachSubscription.objects.filter(coach=coach).exists()
        event_a = _real_sub_event(
            "customer.subscription.created", sub_id="sub_1", status="active"
        )
        event_b = _real_sub_event(
            "customer.subscription.deleted", sub_id="sub_1", status="canceled"
        )

        errors = _race(event_a, event_b, BEFORE_MIRROR_WRITE)

        assert errors == [], f"worker thread(s) raised: {errors}"
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED
        assert sub.stripe_subscription_id == "sub_1"
        started = _events(EventName.SUBSCRIPTION_STARTED)
        cancelled = _events(EventName.SUBSCRIPTION_CANCELLED)
        assert len(started) == 1
        assert started[0].props["subscription"] == "sub_1"
        assert len(cancelled) == 1
        assert cancelled[0].props["subscription"] == "sub_1"
