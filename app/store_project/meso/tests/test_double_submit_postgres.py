r"""PostgreSQL-only regression tests for concurrent double-submits (#540).

Four call sites share the same check-then-act shape: read a status, decide
it's actionable, then act — with no row lock between the read and the act.
Two concurrent submits of the SAME thing (a double-tap on the agent review
screen's Apply button, two overlapping trial starts, a double-submitted
"request my coach" form) both read the pre-mutation state and both act:

- ``batch_apply``/``batch_dismiss`` (views.py) — both read ``PENDING`` and
  both apply/dismiss: an ``add`` change's row is created twice, two "Applied
  agent changes" undo ``PlanAction``\ s are recorded, and two ``batch_applied``
  analytics events fire. An Apply racing a Dismiss can otherwise leave applied
  rows sitting under a ``DISMISSED`` batch.
- ``CoachSubscription.start_trial_for`` (models.py) — both read a ``free``
  row and both start a trial, doubling the ``subscription_started`` event.
- ``athlete_request_coach`` (views.py) — the ``unique_coach_athlete``
  constraint stops a duplicate CoachAthlete *row*, but not the duplicate SIDE
  EFFECTS: a second ``coach_request_sent`` event and a second notification
  email for the same request.

Each gets a ``select_for_update()`` lock + a re-check of the same condition
under the lock, taken in the same order ``billing.webhooks._lock_mirror``
already uses (#546): the row itself if it exists, else the relevant User row,
then re-read — so the loser sees what the winner just committed and backs off
cleanly instead of repeating it.

Two more cases from the same change:

- the two user-row mutex locks above (``athlete_request_coach`` and
  ``CoachSubscription.start_trial_for``) used a plain ``select_for_update()``.
  Postgres FKs are ``DEFERRABLE INITIALLY DEFERRED``, so a concurrent
  insert/update referencing that user row takes ``FOR KEY SHARE`` on it at
  **commit**, and a plain ``FOR UPDATE`` blocks that — deadlocking against,
  e.g., an invite claim materializing a link for the same athlete, or two
  coaches requesting each other at once. ``select_for_update(no_key=True)``
  (``FOR NO KEY UPDATE``) still serializes same-user double submits but
  doesn't block a commit-time ``FOR KEY SHARE``.
- ``change_set_status`` (the approve/reject endpoint) checked the batch's
  status without a lock, so a reject racing an Apply could land its write
  after the Apply committed, leaving an *applied* change marked REJECTED.

**Why this file exists separately.** ``select_for_update`` is a documented
no-op on SQLite, and the default in-memory SQLite test database doesn't even
share rows across threads/connections — each thread gets its own private
database, so two threads can never contend for the same row lock at all. A
missing lock and a working one would look identical there: a test for lock
*contention* is simply not expressible on SQLite. Same blind spot as
``test_billing_webhook_postgres.py`` and ``test_settle_postgres.py``.

Run locally against the dev Postgres (``just services``)::

    TEST_DATABASE_URL=postgres://postgres:postgres@localhost:5434/postgres \
        uv run pytest app/store_project/meso/tests/test_double_submit_postgres.py -v
"""

import contextlib
import json
import threading
from unittest import mock

import pytest
import stripe
from django.db import connection
from django.test import Client
from django.urls import reverse

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import sandbox as meso_sandbox
from store_project.meso import views
from store_project.meso.agent import apply as agent_apply
from store_project.meso.billing import access as billing_access
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
from store_project.meso.models import InvalidTransition
from store_project.meso.models import PlanAction
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.meso.tests.test_billing_stripe import GATEWAY_CHECKOUT
from store_project.meso.tests.test_billing_stripe import GATEWAY_SESSION_EXPIRE
from store_project.meso.tests.test_billing_stripe import GATEWAY_SESSION_LIST
from store_project.meso.tests.test_billing_stripe import GATEWAY_SUB_LIST
from store_project.meso.tests.test_billing_stripe import _session_list
from store_project.meso.tests.test_billing_stripe import _subscription_list
from store_project.meso.tests.test_billing_webhook_postgres import _paused
from store_project.meso.tests.test_requests import make_coach
from store_project.payments.utils import stripe_customer_get_or_create
from store_project.users.factories import UserFactory

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


def _events(name):
    """Every ``Event`` row of ``name``, oldest first (deterministic order)."""
    return list(Event.objects.filter(name=name).order_by("id"))


def _http_race(requests, hooks, timeout=5, errors=None):
    """POST each ``(client, url, data[, content_type])`` at once, meeting at ``hooks``.

    Mirrors ``test_billing_webhook_postgres._race``: thread A starts first and
    pauses at a hook; thread B starts once A has reached it. For a hook before
    the lock, both threads meet there and then B blocks on the row lock until
    A commits. For a hook after it, B blocks before reaching the hook, A's wait
    times out and A commits. Either way B then acts on what A committed.
    Without the fix, nothing blocks B, and both act on the same stale reads.

    Each request is a 3-tuple, or a 4-tuple whose last element is a
    ``content_type`` (e.g. ``"application/json"``, with ``data`` already a JSON
    string) instead of the default form-encoding.

    A deadlock surfaces as an exception raised inside the view, and Django's
    test ``Client`` re-raises it — which, inside a worker thread, would
    otherwise just print a traceback and vanish. Pass a same-length list as
    ``errors`` to capture each thread's exception (or ``None``) into it instead;
    left as ``None`` (the default), an exception propagates out of the thread
    as before and this function's own assertions below are what catch it (via
    ``responses[i]`` staying unset).

    Returns the responses, in the same order as ``requests``.
    """
    a_reached = threading.Event()
    responses = [None] * len(requests)

    def run(i, req):
        client, url, data, *rest = req
        kwargs = {"content_type": rest[0]} if rest else {}
        try:
            responses[i] = client.post(url, data or {}, **kwargs)
        except Exception as exc:  # noqa: BLE001 - captured for the assertions
            if errors is None:
                raise
            errors[i] = exc
        finally:
            connection.close()

    with contextlib.ExitStack() as stack:
        for owner, name, when in hooks:
            wrapper = _paused(getattr(owner, name), when, a_reached)
            stack.enter_context(mock.patch.object(owner, name, wrapper))
        threads = [
            threading.Thread(target=run, args=(i, req))
            for i, req in enumerate(requests)
        ]
        threads[0].start()
        assert a_reached.wait(timeout=timeout), "thread A never reached a hook"
        for t in threads[1:]:
            t.start()
        for t in threads:
            t.join(timeout=timeout * 2)

    assert all(not t.is_alive() for t in threads), "a worker thread did not finish"
    return responses


# ---------------------------------------------------------------------------
# batch_apply / batch_dismiss
# ---------------------------------------------------------------------------

#: Fires after the view's billing gate — before either the unlocked pre-check
#: or (in the fix) the row lock, so both threads always rendezvous cleanly.
CAN_EDIT_PLAN_AFTER = (billing_access, "can_edit_plan", "after")
#: Shared by both ``batch_apply`` and ``batch_dismiss`` — both call this to
#: fetch the batch before anything else, so it's a valid rendezvous point for
#: an Apply racing a Dismiss too.
COACH_BATCH_LOOKUP_AFTER = (views, "_coach_batch_or_404", "after")


def _make_batch_with_add():
    """A PENDING batch with one ``add`` change — applying it creates a new row."""
    plan, session, cell = make_plan()  # one starter row at order 0
    batch = AgentProposalBatchFactory(plan=plan, coach=plan.coach)
    ProposedChangeFactory(
        batch=batch,
        kind=ProposedChange.Kind.ADD,
        prescription=None,
        session=session,
        payload={
            "name": "Romanian Deadlift",
            "sets": "3",
            "reps": "8-10",
            "rpe": "7",
        },
    )
    return plan, session, cell, batch


class TestBatchApplyDoubleSubmit:
    def test_two_concurrent_applies_exactly_one_wins(self):
        plan, session, _cell, batch = _make_batch_with_add()
        coach = plan.coach
        apply_url = reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        client_a, client_b = Client(), Client()
        client_a.force_login(coach)
        client_b.force_login(coach)

        resp_a, resp_b = _http_race(
            [(client_a, apply_url, None), (client_b, apply_url, None)],
            [CAN_EDIT_PLAN_AFTER],
        )

        statuses = sorted([resp_a.status_code, resp_b.status_code])
        assert statuses == [200, 409], (resp_a.status_code, resp_b.status_code)
        loser = resp_a if resp_a.status_code == 409 else resp_b
        assert loser.json() == {
            "ok": False,
            "error": "This batch has already been resolved.",
        }
        # The starter row from make_plan() plus exactly ONE added row.
        assert session.cells().count() == 2
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED
        actions = PlanAction.objects.filter(
            plan=plan,
            stack=PlanAction.Stack.UNDO,
            label="Applied agent changes",
        )
        assert actions.count() == 1
        assert len(_events(EventName.BATCH_APPLIED)) == 1


class TestBatchApplyRacesDismiss:
    def test_apply_racing_dismiss_never_leaves_applied_rows_under_dismissed(self):
        plan, session, _cell, batch = _make_batch_with_add()
        coach = plan.coach
        apply_url = reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        dismiss_url = reverse("meso:api_batch_dismiss", kwargs={"batch_id": batch.pk})
        client_a, client_b = Client(), Client()
        client_a.force_login(coach)
        client_b.force_login(coach)

        _http_race(
            [(client_a, apply_url, None), (client_b, dismiss_url, None)],
            [COACH_BATCH_LOOKUP_AFTER],
        )

        batch.refresh_from_db()
        added_rows = session.cells().count() - 1  # minus the starter row
        if batch.status == AgentProposalBatch.Status.APPLIED:
            assert added_rows == 1
        elif batch.status == AgentProposalBatch.Status.DISMISSED:
            assert added_rows == 0
        else:
            pytest.fail(f"unexpected batch status: {batch.status}")


# ---------------------------------------------------------------------------
# CoachSubscription.start_trial_for
# ---------------------------------------------------------------------------

#: Fires right before the check-then-act in ``start_trial`` itself — common to
#: both the locked and unlocked implementations, so it's a stable rendezvous
#: point regardless of which one is running.
START_TRIAL_BEFORE = (CoachSubscription, "start_trial", "before")


def _race_start_trial(coach):
    a_reached = threading.Event()
    results = []
    errors = []

    def run():
        try:
            results.append(CoachSubscription.start_trial_for(coach))
        except Exception as exc:  # noqa: BLE001 - captured for the assertions
            errors.append(exc)
        finally:
            connection.close()

    owner, name, when = START_TRIAL_BEFORE
    wrapper = _paused(getattr(owner, name), when, a_reached)
    with mock.patch.object(owner, name, wrapper):
        thread_a = threading.Thread(target=run)
        thread_a.start()
        assert a_reached.wait(timeout=5), "thread A never reached the hook"
        thread_b = threading.Thread(target=run)
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

    assert not thread_a.is_alive(), "thread A did not finish"
    assert not thread_b.is_alive(), "thread B did not finish"
    return results, errors


class TestStartTrialForDoubleSubmit:
    def test_no_row_yet_one_wins_one_invalid_transition(self):
        coach = UserFactory()
        assert not CoachSubscription.objects.filter(coach=coach).exists()

        results, errors = _race_start_trial(coach)

        assert len(results) == 1, results
        assert len(errors) == 1, errors
        assert isinstance(errors[0], InvalidTransition)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1

    def test_existing_free_row_one_wins_one_invalid_transition(self):
        coach = UserFactory()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.FREE)

        results, errors = _race_start_trial(coach)

        assert len(results) == 1, results
        assert len(errors) == 1, errors
        assert isinstance(errors[0], InvalidTransition)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1


# ---------------------------------------------------------------------------
# athlete_request_coach
# ---------------------------------------------------------------------------

#: Common to both the locked and unlocked implementations — the one statement
#: every non-early-return path always reaches exactly once.
COACH_ATHLETE_REQUEST_BEFORE = (CoachAthlete, "request", "before")


class TestAthleteRequestCoachDoubleSubmit:
    def test_no_existing_link_one_event_one_email(self):
        coach = make_coach()
        athlete = UserFactory()
        url = reverse("meso:athlete_request_coach")
        client_a, client_b = Client(), Client()
        client_a.force_login(athlete)
        client_b.force_login(athlete)
        data = {"email": coach.email}

        with mock.patch(
            "store_project.meso.views.send_coach_request_email", return_value=True
        ) as send_mock:
            resp_a, resp_b = _http_race(
                [(client_a, url, data), (client_b, url, data)],
                [COACH_ATHLETE_REQUEST_BEFORE],
            )

        assert resp_a.status_code == 302
        assert resp_b.status_code == 302
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).count() == 1
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert len(_events(EventName.COACH_REQUEST_SENT)) == 1
        assert send_mock.call_count == 1

    def test_existing_closed_link_one_event_one_email(self):
        coach = make_coach()
        athlete = UserFactory()
        link = CoachAthlete.request(athlete=athlete, coach=coach)
        link.decline()
        url = reverse("meso:athlete_request_coach")
        client_a, client_b = Client(), Client()
        client_a.force_login(athlete)
        client_b.force_login(athlete)
        data = {"email": coach.email}

        with mock.patch(
            "store_project.meso.views.send_coach_request_email", return_value=True
        ) as send_mock:
            resp_a, resp_b = _http_race(
                [(client_a, url, data), (client_b, url, data)],
                [COACH_ATHLETE_REQUEST_BEFORE],
            )

        assert resp_a.status_code == 302
        assert resp_b.status_code == 302
        link.refresh_from_db()
        assert link.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert CoachAthlete.objects.filter(coach=coach, athlete=athlete).count() == 1
        assert len(_events(EventName.COACH_REQUEST_SENT)) == 1
        assert send_mock.call_count == 1


# ---------------------------------------------------------------------------
# The two user-row mutexes deadlock on a plain FOR UPDATE (#540)
# ---------------------------------------------------------------------------
#
# Postgres FKs are DEFERRABLE INITIALLY DEFERRED, so a transaction that
# inserts/updates a row referencing a user takes FOR KEY SHARE on that user
# row at COMMIT, not at the INSERT/UPDATE. FOR UPDATE blocks FOR KEY SHARE;
# FOR NO KEY UPDATE (``select_for_update(no_key=True)``) doesn't, while still
# conflicting with another FOR NO KEY UPDATE/FOR UPDATE — same-user double
# submits still serialize.

#: T1 (the invite claim) pauses right after ``CoachInvite.accept`` has
#: inserted the CoachAthlete row and updated the invite — both uncommitted —
#: so the claim's commit (and its FK checks) is still pending when T2 starts.
INVITE_ACCEPT_AFTER = (CoachInvite, "accept", "after")


class TestAthleteRequestRacesInviteClaim:
    """An invite claim racing the athlete's own coach request (#540).

    Athlete U holds a pending ``CoachInvite`` from coach C, no ``CoachAthlete``
    row yet. T1 = U's claim (``action=accept``) inserts the link and updates
    the invite, uncommitted. T2 = U's own ``athlete_request_coach`` for C's
    email locks U's row (the "no existing link yet" branch) and, seeing no
    committed link, attempts its own insert of the same ``(coach, athlete)``
    pair — which blocks behind T1's uncommitted insert. T1's commit then needs
    FOR KEY SHARE on U (``CoachAthlete.athlete`` / ``CoachInvite.accepted_by``
    both reference U) — a plain FOR UPDATE on U from T2 blocks that, and T2 is
    itself blocked on T1: deadlock. ``no_key=True`` breaks the cycle.
    """

    def test_claim_and_request_do_not_deadlock(self):
        coach = make_coach()
        athlete = UserFactory()
        invite, _created = CoachInvite.open_for(coach=coach, email=athlete.email)
        claim_url = reverse("meso:invite_claim", kwargs={"token": invite.token})
        request_url = reverse("meso:athlete_request_coach")
        client_a, client_b = Client(), Client()
        client_a.force_login(athlete)
        client_b.force_login(athlete)
        errors = [None, None]

        with mock.patch(
            "store_project.meso.views.send_coach_request_email", return_value=True
        ):
            responses = _http_race(
                [
                    (client_a, claim_url, {"action": "accept"}),
                    (client_b, request_url, {"email": coach.email}),
                ],
                [INVITE_ACCEPT_AFTER, COACH_ATHLETE_REQUEST_BEFORE],
                errors=errors,
            )

        assert errors == [None, None], errors
        assert responses[0].status_code == 302
        assert responses[1].status_code == 302
        # The claim wins the race (it reaches its hook first and so commits
        # first): the link is ACTIVE via the invite's own ``invited_by=coach``.
        # The request's insert falls back to that row and leaves it unchanged
        # (it still records its event and sends its email, as on main).
        link = CoachAthlete.objects.get(coach=coach, athlete=athlete)
        assert link.status == CoachAthlete.Status.ACTIVE
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.ACCEPTED
        assert invite.accepted_by == athlete


class TestMutualCoachRequests:
    """Two coaches requesting each other at the same moment (#540).

    Coaches X and Y (both have a ``CoachProfile``) request each other at the
    same moment. Each locks its OWN user row and inserts a link whose
    ``coach_id`` is the OTHER coach — two distinct rows, no unique-constraint
    contention between them. At commit, each needs FOR KEY SHARE on the
    other's (locked) row: a plain FOR UPDATE deadlocks; FOR NO KEY UPDATE
    doesn't.
    """

    def test_no_deadlock_two_links_two_events(self):
        coach_x = make_coach(email="coachx@example.com", name="Coach X")
        coach_y = make_coach(email="coachy@example.com", name="Coach Y")
        url = reverse("meso:athlete_request_coach")
        client_x, client_y = Client(), Client()
        client_x.force_login(coach_x)
        client_y.force_login(coach_y)
        errors = [None, None]

        with mock.patch(
            "store_project.meso.views.send_coach_request_email", return_value=True
        ):
            responses = _http_race(
                [
                    (client_x, url, {"email": coach_y.email}),
                    (client_y, url, {"email": coach_x.email}),
                ],
                [COACH_ATHLETE_REQUEST_BEFORE],
                errors=errors,
            )

        assert errors == [None, None], errors
        assert responses[0].status_code == 302
        assert responses[1].status_code == 302
        y_coaches_x = CoachAthlete.objects.get(coach=coach_y, athlete=coach_x)
        x_coaches_y = CoachAthlete.objects.get(coach=coach_x, athlete=coach_y)
        assert y_coaches_x.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert x_coaches_y.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST
        assert CoachAthlete.objects.count() == 2
        assert len(_events(EventName.COACH_REQUEST_SENT)) == 2


# ---------------------------------------------------------------------------
# change_set_status races batch_apply (#540)
# ---------------------------------------------------------------------------

#: Fires after ``apply_batch`` has saved its changes APPROVED and the batch
#: APPLIED — still inside ``batch_apply``'s own transaction, so both writes
#: are uncommitted and the batch row lock is still held.
APPLY_BATCH_AFTER = (agent_apply, "apply_batch", "after")


class TestChangeSetStatusRacesApply:
    """A reject racing an Apply on the same batch (#540).

    ``change_set_status`` checked ``batch.status != PENDING`` without a lock,
    so a reject that read PENDING just before an Apply committed would write
    REJECTED over a change the Apply had just approved and applied — on a
    batch that was, by the time the reject's write landed, already APPLIED.
    """

    def test_reject_after_apply_commits_gets_409_not_a_lost_update(self):
        plan, _session, _cell, batch = _make_batch_with_add()
        coach = plan.coach
        change = batch.changes.get()
        apply_url = reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        status_url = reverse("meso:api_change_status", kwargs={"pk": change.pk})
        client_a, client_b = Client(), Client()
        client_a.force_login(coach)
        client_b.force_login(coach)

        responses = _http_race(
            [
                (client_a, apply_url, None),
                (
                    client_b,
                    status_url,
                    json.dumps({"status": "rejected"}),
                    "application/json",
                ),
            ],
            [APPLY_BATCH_AFTER],
        )

        resp_apply, resp_reject = responses
        assert resp_apply.status_code == 200, resp_apply.content
        assert resp_reject.status_code == 409, resp_reject.content
        assert resp_reject.json() == {
            "ok": False,
            "error": "This batch has already been resolved.",
        }
        batch.refresh_from_db()
        assert batch.status == AgentProposalBatch.Status.APPLIED
        change.refresh_from_db()
        assert change.status == ProposedChange.Status.APPROVED


# ---------------------------------------------------------------------------
# billing_subscribe (adversarial review of #556)
# ---------------------------------------------------------------------------
#
# Two concurrent Subscribe POSTs for one coach (two tabs, or a double submit)
# both pass the new Stripe pre-checks and both create a Checkout Session, so
# the coach can complete two and be billed twice. Worse, for a coach with no
# ``stripe_customer_id`` yet, both ``customer_has_open_subscription`` and
# ``expire_open_subscription_checkouts`` early-return with no Stripe call at
# all, so each request's own ``stripe_customer_get_or_create`` creates a
# DIFFERENT Stripe customer and overwrites ``User.stripe_customer_id`` — the
# loser's customer becomes invisible to the mirror, the guard, and the Portal
# forever.
#
# Round 2 of the adversarial review (Fix A) moved the customer creation OUT
# of the per-coach lock (so the id write is durable even if the worker dies
# before the locked Checkout section commits) and made
# ``stripe_customer_get_or_create`` write-once instead of lock-protected: two
# racers with a stale (empty) in-memory id can now BOTH create a Stripe
# customer — a genuinely concurrent race is no longer serialized away by the
# lock the way it was before. The safety property is narrower but still
# real: only ONE of those customers ever gets attached to the coach (a
# conditional ``UPDATE ... WHERE stripe_customer_id = ''``), so the loser's
# is an unused, harmless orphan in Stripe — every Checkout Session either
# racer actually creates still uses the SAME (winning) customer.

#: Fires right after the sandbox gate, before the checks this fixes touch —
#: common to both the locked and unlocked implementations, so it's a stable
#: rendezvous point regardless of which one is running.
IS_SANDBOX_AFTER = (meso_sandbox, "is_sandbox", "after")


class _FakeStripeCustomersAndSessions:
    """A tiny in-memory Stripe double the two racing requests both see.

    The canned ``stripe.ListObject`` builders elsewhere
    (``_session_list``/``_subscription_list``) return a fixed snapshot; this
    fake instead tracks real mutable state — customers created and Checkout
    Sessions' open/expired status — so the SECOND request's
    ``Session.list(status="open")`` genuinely sees what the FIRST request
    just committed, the way real Stripe would. A lock guards it since both
    threads call into it concurrently.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.customers = []
        self._next_customer = 0
        self._sessions = {}
        self._next_session = 0

    def create_customer(self, *, email=None, id=None, **kwargs):
        with self._lock:
            cus_id = id or f"cus_{self._next_customer}"
            self._next_customer += 1
            self.customers.append(cus_id)
        return mock.Mock(id=cus_id)

    def retrieve_customer(self, customer_id, **kwargs):
        with self._lock:
            known = customer_id in self.customers
        if not known:
            raise stripe.error.InvalidRequestError(
                "No such customer", "id", code="resource_missing"
            )
        return mock.Mock(id=customer_id)

    def list_subscriptions(self, *, customer, status, limit):
        # No real Subscription is ever created in this race — only Checkout
        # Sessions — so there's never a live subscription to report.
        return _subscription_list([])

    def list_sessions(self, *, customer, status, limit):
        with self._lock:
            matches = [
                (s["id"], s["mode"])
                for s in self._sessions.values()
                if s["customer"] == customer and s["status"] == status
            ]
        return _session_list(matches)

    def expire_session(self, session_id):
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["status"] = "expired"

    def create_session(self, *, customer, mode, **kwargs):
        with self._lock:
            sid = f"cs_{self._next_session}"
            self._next_session += 1
            self._sessions[sid] = {
                "id": sid,
                "customer": customer,
                "mode": mode,
                "status": "open",
            }
        return mock.Mock(url=f"https://stripe.test/{sid}", id=sid)

    def open_subscription_session_count(self):
        with self._lock:
            return sum(
                1
                for s in self._sessions.values()
                if s["status"] == "open" and s["mode"] == "subscription"
            )

    def session_customers(self):
        """The distinct customer id every Checkout Session was created under."""
        with self._lock:
            return {s["customer"] for s in self._sessions.values()}


class TestBillingSubscribeDoubleSubmit:
    def test_one_customer_wins_and_every_checkout_session_uses_it(self, settings):
        """Two racers may each mint a Stripe customer; only one is ever used.

        Fix A (round 2 of the adversarial review) creates the customer BEFORE
        the per-coach lock, so this race is no longer serialized away — both
        racers can genuinely call ``stripe.Customer.create`` when they both
        read a stale, empty ``stripe_customer_id``. What the write-once
        conditional update still guarantees is narrower: only ONE customer
        ever gets attached to the coach, and every Checkout Session either
        racer actually creates (there and any later, idempotent
        ``stripe_customer_get_or_create`` calls in the same request) uses
        that SAME customer — the other is an unused, harmless orphan in
        Stripe.
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = make_coach()
        assert coach.stripe_customer_id == ""
        url = reverse("meso:billing_subscribe")
        client_a, client_b = Client(), Client()
        client_a.force_login(coach)
        client_b.force_login(coach)
        fake = _FakeStripeCustomersAndSessions()

        with (
            mock.patch(
                "store_project.payments.utils.stripe.Customer.create",
                side_effect=fake.create_customer,
            ),
            mock.patch(
                "store_project.payments.utils.stripe.Customer.retrieve",
                side_effect=fake.retrieve_customer,
            ),
            mock.patch(GATEWAY_SUB_LIST, side_effect=fake.list_subscriptions),
            mock.patch(GATEWAY_SESSION_LIST, side_effect=fake.list_sessions),
            mock.patch(GATEWAY_SESSION_EXPIRE, side_effect=fake.expire_session),
            mock.patch(GATEWAY_CHECKOUT, side_effect=fake.create_session),
        ):
            resp_a, resp_b = _http_race(
                [(client_a, url, None), (client_b, url, None)],
                [IS_SANDBOX_AFTER],
            )

        assert resp_a.status_code == 302
        assert resp_b.status_code == 302
        # At most one customer per racer — never more, and never a THIRD one
        # from some later re-read.
        assert len(fake.customers) <= 2, fake.customers
        coach.refresh_from_db()
        # The coach ends up pointed at exactly one of the customers created.
        assert coach.stripe_customer_id in fake.customers
        # Every Checkout Session either racer created — including any from a
        # later, idempotent ``stripe_customer_get_or_create`` re-read in the
        # same request — used that SAME customer. An orphan is never
        # attached to a real Checkout.
        assert fake.session_customers() == {coach.stripe_customer_id}
        # The loser's earlier session was expired; at most the winner's
        # newer one is still completable.
        assert fake.open_subscription_session_count() <= 1


# ---------------------------------------------------------------------------
# stripe_customer_get_or_create (adversarial review of #556, round 2, Fix A)
# ---------------------------------------------------------------------------
#
# The deterministic version of this race lives in
# ``payments/tests/test_utils.py`` (a caller whose in-memory ``user`` still
# reads "" because another writer already committed). This is the real,
# concurrent-threads version: two genuinely separate Python processes'-worth
# of state (two separately fetched ``User`` instances for the same row, two
# real threads/connections) racing the SAME write-once conditional update.

#: Fires right before either thread calls ``stripe.Customer.create`` — both
#: still hold their own stale (empty) in-memory ``stripe_customer_id`` at
#: this point, which is exactly the race this fix has to survive.
CUSTOMER_CREATE_BEFORE = "before"


class TestStripeCustomerGetOrCreatePostgresRace:
    def test_two_real_threads_racing_converge_on_one_customer(self):
        coach = UserFactory()
        assert coach.stripe_customer_id == ""
        # Two independent reads of the same row — mirrors two separate
        # requests, each with its own request-scoped ``user`` instance.
        user_a = type(coach).objects.get(pk=coach.pk)
        user_b = type(coach).objects.get(pk=coach.pk)
        a_reached = threading.Event()
        results = []
        errors = []
        created_ids = iter(["cus_thread_a", "cus_thread_b"])

        def fake_create(**kwargs):
            return mock.Mock(id=next(created_ids))

        wrapped_create = _paused(fake_create, CUSTOMER_CREATE_BEFORE, a_reached)

        def run(user):
            try:
                results.append(stripe_customer_get_or_create(user))
            except Exception as exc:  # noqa: BLE001 - captured for the assertions
                errors.append(exc)
            finally:
                connection.close()

        with (
            mock.patch(
                "store_project.payments.utils.stripe.Customer.create",
                side_effect=wrapped_create,
            ),
            mock.patch(
                "store_project.payments.utils.stripe.Customer.retrieve",
                side_effect=lambda cid, **kw: mock.Mock(id=cid),
            ),
        ):
            thread_a = threading.Thread(target=run, args=(user_a,))
            thread_a.start()
            assert a_reached.wait(timeout=5), "thread A never reached the hook"
            thread_b = threading.Thread(target=run, args=(user_b,))
            thread_b.start()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)

        assert not thread_a.is_alive(), "thread A did not finish"
        assert not thread_b.is_alive(), "thread B did not finish"
        assert errors == [], errors
        assert len(results) == 2
        # Both calls return the SAME customer — the loser discarded the one
        # it minted and re-read the winner's instead.
        assert results[0].id == results[1].id
        winner_id = results[0].id
        assert winner_id in ("cus_thread_a", "cus_thread_b")
        coach.refresh_from_db()
        assert coach.stripe_customer_id == winner_id
