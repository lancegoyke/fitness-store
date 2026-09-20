"""PostgreSQL regressions for Plan/CoachAthlete creator locks (#596)."""

import threading
import time

import pytest
from django.db import DatabaseError
from django.db import connection
from django.db import transaction
from django.test import Client
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.tests.test_batch_deliver import comp
from store_project.meso.tests.test_batch_deliver import seed_source
from store_project.meso.tests.test_template_plans import template_plan
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row locks are not observable on SQLite.",
    ),
]


def _wait_until_a_backend_is_lock_blocked(timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
            (blocked,) = cursor.fetchone()
            if blocked:
                return True
            time.sleep(interval)
    return False


def _hold_rows(model, pks, locked, release, errors):
    try:
        with transaction.atomic():
            list(
                model.objects.select_for_update(no_key=True)
                .filter(pk__in=pks)
                .order_by("pk")
            )
            locked.set()
            assert release.wait(timeout=8)
    except Exception as exc:  # pragma: no cover - surfaced below
        errors.append(exc)
    finally:
        connection.close()


def _run_while_rows_are_held(model, pks, request_call):
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    request_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_rows,
        args=(model, pks, locked, release, holder_errors),
    )

    def run_request():
        try:
            result["response"] = request_call()
        except Exception as exc:  # pragma: no cover - surfaced below
            request_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_request)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    was_blocked = _wait_until_a_backend_is_lock_blocked()
    release.set()
    holder.join(timeout=10)
    worker.join(timeout=10)
    return was_blocked, holder_errors, request_errors, result, holder, worker


def test_template_use_waits_for_the_target_link_lock():
    coach = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    template, _ = template_plan(coach, title="Locked template")

    def post_template():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:template_use", kwargs={"plan_id": template.pk}),
            {"relationship": relationship.pk},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(CoachAthlete, [relationship.pk], post_template)
    )

    assert blocked, "template_use did not take the target CoachAthlete lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_template_use_does_not_lock_the_athlete_user_row():
    """The link lock is `OF SELF`, so it never locks the athlete's User row.

    `select_related("athlete")` would join `users_user` and lock that row too —
    CoachAthlete then User, the inversion of a User-rooted cascade
    (`lock_cascade_parents` takes User first).
    """
    coach = UserFactory()
    athlete = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=athlete)
    template, _ = template_plan(coach, title="Joined-row template")

    def post_template():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:template_use", kwargs={"plan_id": template.pk}),
            {"relationship": relationship.pk},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [athlete.pk], post_template)
    )

    assert not blocked, "template_use locked the athlete's User row via its join"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_plan_create_draft_takes_the_coach_lock_before_the_link():
    """A draft must not hold the link while it waits for the coach row.

    `clear_demo` holds the coach row and then wants the demo links (#590), so a
    draft `plan_create` that took the link first and `_reserve_plan_draft`'s coach
    lock second ran link -> User against it: a deadlock. Post-fix the draft waits
    on the coach row holding nothing, so another transaction can still take the
    link while it waits.
    """
    coach = UserFactory()
    relationship = CoachAthleteFactory(coach=coach, athlete=UserFactory())
    # Log in BEFORE the coach row is held: the login signal UPDATEs that row.
    client = Client()
    client.force_login(coach)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    request_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_rows, args=(User, [coach.pk], locked, release, holder_errors)
    )

    def post_draft():
        try:
            result["response"] = client.post(
                reverse("meso:plan_create", kwargs={"pk": relationship.athlete_id}),
                {"draft": "1"},
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            request_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=post_draft)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    blocked = _wait_until_a_backend_is_lock_blocked()
    link_is_free = False
    try:
        with transaction.atomic():
            list(
                CoachAthlete.objects.select_for_update(no_key=True, nowait=True).filter(
                    pk=relationship.pk
                )
            )
        link_is_free = True
    except DatabaseError:
        pass
    finally:
        release.set()
        holder.join(timeout=10)
        worker.join(timeout=10)

    assert blocked, "plan_create(draft) never waited for the coach row"
    assert link_is_free, "plan_create(draft) held the link while waiting for the coach"
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert relationship.plans.count() == 1


def test_plan_batch_deliver_waits_for_all_target_link_locks():
    coach = comp(UserFactory())
    source, _ = seed_source(coach=coach)
    targets = [
        CoachAthleteFactory(coach=coach, athlete=UserFactory()),
        CoachAthleteFactory(coach=coach, athlete=UserFactory()),
    ]

    def post_batch():
        client = Client()
        client.force_login(coach)
        return client.post(
            reverse("meso:plan_batch_deliver", kwargs={"plan_id": source.pk}),
            {"relationships": [target.pk for target in targets]},
        )

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(
            CoachAthlete, [target.pk for target in targets], post_batch
        )
    )

    assert blocked, "plan_batch_deliver did not lock its target links"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert all(target.plans.count() == 1 for target in targets)


def test_roster_add_self_waits_for_the_coach_user_lock():
    coach = UserFactory()
    client = Client()
    client.force_login(coach)

    def post_add_self():
        return client.post(reverse("meso:roster_add_self"))

    blocked, holder_errors, request_errors, result, holder, worker = (
        _run_while_rows_are_held(User, [coach.pk], post_add_self)
    )

    assert blocked, "roster_add_self did not take the coach User lock"
    assert not holder.is_alive() and not worker.is_alive()
    assert holder_errors == []
    assert request_errors == []
    assert result["response"].status_code == 302
    assert CoachAthlete.objects.filter(
        coach=coach, athlete=coach, is_self=True
    ).exists()
