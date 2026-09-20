"""PostgreSQL regressions for parent-mutex lock strength (#589)."""

import threading
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db import transaction

from store_project.meso import history
from store_project.meso import views
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.users.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="PostgreSQL row-lock strengths are not observable on SQLite.",
    ),
]


def _set_short_lock_timeout():
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '750ms'")


def test_record_plan_action_does_not_block_a_deferred_plan_fk_commit():
    plan = PlanFactory()
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    insert_errors = []

    def hold_recording_transaction():
        try:
            with transaction.atomic():
                history.record_plan_action(plan, "Concurrent edit")
                locked.set()
                assert release.wait(timeout=5)
        except Exception as exc:  # pragma: no cover - surfaced below
            holder_errors.append(exc)
        finally:
            connection.close()

    def insert_referencing_batch():
        try:
            _set_short_lock_timeout()
            with transaction.atomic():
                AgentProposalBatchFactory(plan=plan, coach=plan.coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            insert_errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_recording_transaction)
    inserter = threading.Thread(target=insert_referencing_batch)
    holder.start()
    assert locked.wait(timeout=5), "record_plan_action never acquired its Plan lock"
    inserter.start()
    inserter.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    inserter.join(timeout=5)

    assert not holder.is_alive()
    assert not inserter.is_alive()
    assert holder_errors == []
    assert insert_errors == [], (
        "a deferred AgentProposalBatch.plan FK commit was blocked by the "
        f"Plan mutex: {insert_errors}"
    )


def test_reserve_plan_draft_does_not_block_a_deferred_user_fk_commit(monkeypatch):
    coach = UserFactory()
    plan = PlanFactory(relationship__coach=coach)
    MesocycleFactory(plan=plan, order=0)
    request = SimpleNamespace(user=coach)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    insert_errors = []

    monkeypatch.setattr(views.meso_sandbox, "is_sandbox", lambda user: False)
    monkeypatch.setattr(views.billing_access, "can_use_agent", lambda user: True)
    monkeypatch.setattr(views.agent_client, "get_default_client", lambda: object())
    monkeypatch.setattr(views.messages, "success", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        views.agent_service, "create_drafting_batch", lambda *args, **kwargs: object()
    )

    def hold_reservation_transaction():
        try:
            with transaction.atomic():
                views._reserve_plan_draft(request, plan)
                locked.set()
                assert release.wait(timeout=5)
        except Exception as exc:  # pragma: no cover - surfaced below
            holder_errors.append(exc)
        finally:
            connection.close()

    def insert_referencing_profile():
        try:
            _set_short_lock_timeout()
            with transaction.atomic():
                CoachProfileFactory(user=coach)
        except Exception as exc:  # pragma: no cover - surfaced below
            insert_errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_reservation_transaction)
    inserter = threading.Thread(target=insert_referencing_profile)
    holder.start()
    assert locked.wait(timeout=5), "_reserve_plan_draft never acquired its User lock"
    inserter.start()
    inserter.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    inserter.join(timeout=5)

    assert not holder.is_alive()
    assert not inserter.is_alive()
    assert holder_errors == []
    assert insert_errors == [], (
        "a deferred CoachProfile.user FK commit was blocked by the coach mutex: "
        f"{insert_errors}"
    )
