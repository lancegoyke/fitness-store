"""PostgreSQL serialization regression for clear-vs-load demo data (#590)."""

import threading
import time

import pytest
from django.db import connection
from django.db import transaction

from store_project.meso import demo
from store_project.meso.factories import CoachProfileFactory
from store_project.users.factories import UserFactory

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


def test_clear_demo_serializes_with_a_segment_loader():
    coach = UserFactory()
    CoachProfileFactory(user=coach)
    mutex_held = threading.Event()
    continue_load = threading.Event()
    load_errors = []
    clear_errors = []

    def run_loader():
        try:
            with transaction.atomic():
                demo._lock(coach)
                mutex_held.set()
                assert continue_load.wait(timeout=8)
                demo.load_athletes(coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            load_errors.append(exc)
        finally:
            connection.close()

    def run_clearer():
        try:
            demo.clear_demo(coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            clear_errors.append(exc)
        finally:
            connection.close()

    loader = threading.Thread(target=run_loader)
    clearer = threading.Thread(target=run_clearer)
    loader.start()
    assert mutex_held.wait(timeout=5), "the loader never acquired the coach mutex"
    clearer.start()
    clear_was_blocked = _wait_until_a_backend_is_lock_blocked()
    continue_load.set()
    loader.join(timeout=10)
    clearer.join(timeout=10)

    assert clear_was_blocked, (
        "clear_demo passed the in-flight loader instead of waiting on its coach mutex"
    )
    assert not loader.is_alive()
    assert not clearer.is_alive()
    assert load_errors == []
    assert clear_errors == []
    assert not demo.has_athletes(coach), (
        "the serialized clear must remove the segment committed ahead of it"
    )
