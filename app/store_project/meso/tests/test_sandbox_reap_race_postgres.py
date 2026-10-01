"""PostgreSQL race: a demo_load during the sandbox reap leaves no orphans (#674)."""

import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from store_project.meso import demo
from store_project.meso import sandbox
from store_project.meso.models import SandboxSession
from store_project.users.models import User

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="The coach row lock is not observable on SQLite.",
    ),
]


def _expired_sandbox():
    coach = sandbox.create_sandbox()
    SandboxSession.objects.filter(user=coach).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    emails = [demo.demo_email(coach, spec["slug"]) for spec in demo.ATHLETES]
    return coach, emails


def test_demo_load_between_the_first_clear_and_the_delete_leaves_no_orphans(
    monkeypatch,
):
    coach, emails = _expired_sandbox()
    first_clear_done = threading.Event()
    load_done = threading.Event()
    real_clear = demo.clear_demo
    calls = []

    def clear_then_pause_once(target):
        real_clear(target)
        if not calls:
            calls.append(1)
            first_clear_done.set()
            # The still-logged-in sandbox's demo_load runs here, to completion.
            assert load_done.wait(timeout=10)

    monkeypatch.setattr(demo, "clear_demo", clear_then_pause_once)
    errors = []

    def reap():
        try:
            assert sandbox.expire_sandboxes() == 1
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    def load():
        try:
            assert first_clear_done.wait(timeout=10)
            demo.load_demo(coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            load_done.set()
            connection.close()

    threads = [threading.Thread(target=reap), threading.Thread(target=load)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert not User.objects.filter(pk=coach.pk).exists()
    assert User.objects.filter(email__in=emails).count() == 0, (
        "demo athletes recreated mid-reap survived the coach delete"
    )


def test_a_load_queued_behind_the_coach_delete_fails_without_orphans(monkeypatch):
    # The load that arrives while the reap holds the coach row finds the coach
    # gone once the delete commits: it raises and rolls back, creating nothing.
    coach, emails = _expired_sandbox()
    in_delete_txn = threading.Event()
    release = threading.Event()
    real_lock_parents = demo.lock_cascade_parents
    calls = []

    def pause_inside_the_delete_transaction(user_ids):
        real_lock_parents(user_ids)
        calls.append(1)
        # Call 1 is the first clear_demo; call 2 is the coach delete's own.
        if len(calls) == 2:
            in_delete_txn.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(
        demo, "lock_cascade_parents", pause_inside_the_delete_transaction
    )
    outcome = {}

    def reap():
        try:
            outcome["reaped"] = sandbox.expire_sandboxes()
        finally:
            connection.close()

    def load():
        try:
            assert in_delete_txn.wait(timeout=10)
            timer = threading.Timer(0.5, release.set)  # let the load queue first
            timer.start()
            demo.load_demo(coach)
            outcome["load"] = "ok"
        except User.DoesNotExist:
            outcome["load"] = "coach gone"
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["load"] = repr(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=reap), threading.Thread(target=load)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert outcome["reaped"] == 1
    assert outcome["load"] == "coach gone"
    assert User.objects.filter(email__in=emails).count() == 0
