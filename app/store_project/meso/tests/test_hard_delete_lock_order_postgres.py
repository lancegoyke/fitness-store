"""PostgreSQL regressions for admin hard-delete parent locking (#587, #610)."""

import re
import threading
import time
import uuid

import pytest
from django.contrib import admin
from django.db import connection
from django.db import transaction
from django.db.models.signals import post_delete
from django.db.utils import OperationalError
from django.test.utils import CaptureQueriesContext

from store_project.meso import demo
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import AthleteProfileFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import Plan
from store_project.meso.models import ProposedChange
from store_project.users.admin import UserAdmin
from store_project.users.factories import UserFactory
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row locks are not observable on SQLite.",
    ),
]


def _set_short_lock_timeouts():
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '3s'")
        cursor.execute("SET deadlock_timeout = '100ms'")


def _wait_until_backend_is_lock_blocked(backend_pid, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s",
                [backend_pid],
            )
            row = cursor.fetchone()
            if row and row[0]:
                return True
            time.sleep(interval)
    return False


def _user_lock_selects(queries):
    return [
        query["sql"]
        for query in queries
        if 'FROM "users_user"' in query["sql"]
        and re.search(r"\bFOR (?:NO KEY )?UPDATE\b", query["sql"])
    ]


def test_user_admin_delete_model_prelocks_plan_before_the_cascade():
    coach = UserFactory()
    athlete = UserFactory()
    link = CoachAthleteFactory(coach=coach, athlete=athlete)
    plan = PlanFactory(relationship=link)
    batch = AgentProposalBatchFactory(plan=plan, coach=coach)
    change = ProposedChangeFactory(batch=batch)
    reached_child_delete = threading.Event()
    release_delete = threading.Event()
    delete_errors = []

    def pause_after_child_delete(sender, instance, **kwargs):
        if instance.pk != change.pk:
            return
        reached_child_delete.set()
        assert release_delete.wait(timeout=8)

    uid = f"test-587-admin-delete-{change.pk}"
    post_delete.connect(
        pause_after_child_delete,
        sender=ProposedChange,
        dispatch_uid=uid,
        weak=False,
    )

    def run_delete():
        try:
            UserAdmin(User, admin.site).delete_model(None, coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            delete_errors.append(exc)
        finally:
            connection.close()

    deleter = threading.Thread(target=run_delete)
    try:
        deleter.start()
        assert reached_child_delete.wait(timeout=5), (
            "the admin cascade never reached ProposedChange.post_delete"
        )
        plan_was_prelocked = False
        try:
            with transaction.atomic():
                Plan.objects.select_for_update(nowait=True).get(pk=plan.pk)
        except OperationalError:
            plan_was_prelocked = True
        release_delete.set()
        deleter.join(timeout=10)
    finally:
        release_delete.set()
        post_delete.disconnect(
            pause_after_child_delete,
            sender=ProposedChange,
            dispatch_uid=uid,
        )

    assert plan_was_prelocked, (
        "UserAdmin began deleting children before reserving the parent Plan row"
    )
    assert not deleter.is_alive()
    assert delete_errors == []
    assert not User.objects.filter(pk=coach.pk).exists()


@pytest.mark.parametrize("coach_marker", ["profile", "relationship"])
def test_lock_coach_mutexes_locks_only_selected_coaches_without_a_join(coach_marker):
    coach = UserFactory()
    athlete = UserFactory()
    unrelated = UserFactory()
    if coach_marker == "profile":
        CoachProfileFactory(user=coach)
        AthleteProfileFactory(user=athlete)
    else:
        CoachAthleteFactory(coach=coach, athlete=athlete)

    with transaction.atomic(), CaptureQueriesContext(connection) as queries:
        locked_pks = demo.lock_coach_mutexes([unrelated.pk, coach.pk, athlete.pk])

    locks = _user_lock_selects(queries.captured_queries)
    assert locked_pks == [coach.pk]
    assert len(locks) == 1
    assert "FOR NO KEY UPDATE" in locks[0]
    assert "FOR UPDATE" not in locks[0]
    assert " JOIN " not in locks[0]
    assert " DISTINCT " not in locks[0]
    assert " OF " not in locks[0]
    assert re.search(r'ORDER BY (?:"users_user"\."id"|1) ASC', locks[0])


def test_lock_coach_mutexes_empty_selection_issues_no_query():
    with CaptureQueriesContext(connection) as queries:
        locked_pks = demo.lock_coach_mutexes([])

    assert locked_pks == []
    assert queries.captured_queries == []


def test_user_admin_bulk_delete_does_not_deadlock_with_clear_demo(monkeypatch):
    athlete = UserFactory(id=uuid.UUID(int=1))
    coach = UserFactory(id=uuid.UUID(int=2))
    CoachProfileFactory(user=coach)
    CoachAthleteFactory(coach=coach, athlete=athlete, is_demo=True)
    coach_locked = threading.Event()
    release_clear = threading.Event()
    admin_backend_ready = threading.Event()
    admin_backend_pid = []
    clear_errors = []
    admin_errors = []
    original_demo_athletes = demo._demo_athletes

    def pause_after_coach_lock(locked_coach):
        coach_locked.set()
        assert release_clear.wait(timeout=8)
        return original_demo_athletes(locked_coach)

    monkeypatch.setattr(demo, "_demo_athletes", pause_after_coach_lock)

    def run_clear_demo():
        try:
            _set_short_lock_timeouts()
            demo.clear_demo(coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            clear_errors.append(exc)
        finally:
            connection.close()

    def run_admin_delete():
        try:
            _set_short_lock_timeouts()
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                admin_backend_pid.append(cursor.fetchone()[0])
            admin_backend_ready.set()
            UserAdmin(User, admin.site).delete_queryset(
                None, User.objects.filter(pk__in=[coach.pk, athlete.pk])
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            admin_errors.append(exc)
        finally:
            connection.close()

    clearer = threading.Thread(target=run_clear_demo)
    deleter = threading.Thread(target=run_admin_delete)
    try:
        clearer.start()
        assert coach_locked.wait(timeout=5), "clear_demo never locked the coach row"
        deleter.start()
        assert admin_backend_ready.wait(timeout=5), (
            "UserAdmin's thread never opened its PostgreSQL connection"
        )
        assert _wait_until_backend_is_lock_blocked(admin_backend_pid[0]), (
            "UserAdmin never waited for clear_demo's coach mutex"
        )
        release_clear.set()
        clearer.join(timeout=10)
        deleter.join(timeout=10)
    finally:
        release_clear.set()

    assert not clearer.is_alive(), "clear_demo's thread never finished"
    assert not deleter.is_alive(), "UserAdmin's thread never finished"
    assert clear_errors == []
    assert admin_errors == []
    assert not User.objects.filter(pk__in=[coach.pk, athlete.pk]).exists()
