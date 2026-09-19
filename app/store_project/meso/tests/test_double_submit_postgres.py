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
import threading
from unittest import mock

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import views
from store_project.meso.billing import access as billing_access
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import InvalidTransition
from store_project.meso.models import PlanAction
from store_project.meso.models import ProposedChange
from store_project.meso.tests.test_agent_validation import make_plan
from store_project.meso.tests.test_billing_webhook_postgres import _paused
from store_project.meso.tests.test_requests import make_coach
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


def _http_race(requests, hooks, timeout=5):
    """POST each ``(client, url, data)`` at once, the threads meeting at ``hooks``.

    Mirrors ``test_billing_webhook_postgres._race``: thread A starts first and
    pauses at a hook; thread B starts once A has reached it. For a hook before
    the lock, both threads meet there and then B blocks on the row lock until
    A commits. For a hook after it, B blocks before reaching the hook, A's wait
    times out and A commits. Either way B then acts on what A committed.
    Without the fix, nothing blocks B, and both act on the same stale reads.
    Returns the responses, in the same order as ``requests``.
    """
    a_reached = threading.Event()
    responses = [None] * len(requests)

    def run(i, client, url, data):
        try:
            responses[i] = client.post(url, data or {})
        finally:
            connection.close()

    with contextlib.ExitStack() as stack:
        for owner, name, when in hooks:
            wrapper = _paused(getattr(owner, name), when, a_reached)
            stack.enter_context(mock.patch.object(owner, name, wrapper))
        threads = [
            threading.Thread(target=run, args=(i, *req))
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
