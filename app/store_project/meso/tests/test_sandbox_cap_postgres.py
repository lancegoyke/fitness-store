"""PostgreSQL race: the global sandbox cap never overshoots (#673)."""

import threading

import pytest
from django.db import connection

from store_project.meso import sandbox
from store_project.meso.models import SandboxSession

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="The advisory creation lock is not observable on SQLite.",
    ),
]


def test_two_concurrent_entries_at_cap_one_mint_exactly_one(settings, monkeypatch):
    settings.MESO_SANDBOX_MAX_CONCURRENT = 1
    # Force the overlap: whoever counts waits for the other to count too. With
    # the creation lock the second creator is still queued on the lock and never
    # reaches this, so the barrier times out (tolerated) and the first goes on.
    # Without the lock both count 0 here and both mint.
    barrier = threading.Barrier(2)
    real_at_capacity = sandbox.at_capacity

    def at_capacity_after_rendezvous():
        over = real_at_capacity()
        try:
            barrier.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass
        return over

    monkeypatch.setattr(sandbox, "at_capacity", at_capacity_after_rendezvous)
    outcomes = []

    def enter():
        try:
            sandbox.create_sandbox()
            outcomes.append("created")
        except sandbox.SandboxBusy:
            outcomes.append("busy")
        except Exception as exc:  # pragma: no cover - surfaced below
            outcomes.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=enter) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["busy", "created"], outcomes
    assert SandboxSession.objects.count() == 1
