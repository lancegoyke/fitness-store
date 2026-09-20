"""PostgreSQL regressions for parent-mutex lock strength (#589, #611)."""

import json
import re
import threading
from types import SimpleNamespace

import pytest
from django.db import DatabaseError
from django.db import connection
from django.db import transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from store_project.meso import history
from store_project.meso import views
from store_project.meso.agent import service as agent_service
from store_project.meso.billing import webhooks
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachInviteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import ProposedChangeFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.models import ProposedChange
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


def _lock_selects(queries, table):
    """Locking SELECTs whose outer query reads ``table``."""
    table_from = f'FROM "{table}"'
    return [
        query["sql"]
        for query in queries
        if table_from in query["sql"]
        and re.search(r"\bFOR (?:NO KEY )?UPDATE\b", query["sql"])
    ]


def _assert_no_key_self_locks(queries, table, *, count=1):
    locks = _lock_selects(queries, table)
    assert len(locks) == count, (
        f"expected {count} locking SELECT(s) on {table}, got {len(locks)}: {locks}"
    )
    for sql in locks:
        assert "FOR NO KEY UPDATE" in sql, f"{table} lock is too strong: {sql}"
        assert "FOR UPDATE" not in sql, f"{table} uses plain FOR UPDATE: {sql}"
        assert " OF " not in sql, f"unjoined {table} lock has an unnecessary OF: {sql}"


def test_start_trial_for_subscription_locks_use_no_key(monkeypatch):
    monkeypatch.setattr("store_project.analytics.track.track", lambda *a, **kw: None)
    existing = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
    missing = UserFactory()

    with CaptureQueriesContext(connection) as queries:
        CoachSubscription.start_trial_for(existing.coach)
        CoachSubscription.start_trial_for(missing)

    # Existing row: one lock. Missing row: the initial read and get_or_create read.
    _assert_no_key_self_locks(
        queries.captured_queries, "meso_coachsubscription", count=3
    )


def test_plan_create_relationship_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:plan_create", kwargs={"pk": relationship.athlete_id})
        )

    assert response.status_code == 302
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachathlete")


def test_relationship_reinvite_lock_uses_no_key(client):
    relationship = CoachAthleteFactory(
        status=CoachAthlete.Status.ENDED,
    )
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:relationship_reinvite", kwargs={"token": relationship.token})
        )

    assert response.status_code == 302
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachathlete")


def test_athlete_request_coach_locks_use_no_key(client, monkeypatch):
    coach = CoachProfileFactory().user
    athlete = UserFactory()
    client.force_login(athlete)
    monkeypatch.setattr(views, "send_coach_request_email", lambda **kwargs: True)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:athlete_request_coach"), {"email": coach.email}
        )

    assert response.status_code == 302
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachathlete", count=2)


def test_coach_invite_revoke_lock_uses_no_key(client):
    invite = CoachInviteFactory()
    client.force_login(invite.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:coach_invite_revoke", kwargs={"token": invite.token})
        )

    assert response.status_code == 302
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachinvite")


def test_coach_invite_resend_lock_uses_no_key(client, monkeypatch):
    invite = CoachInviteFactory()
    client.force_login(invite.coach)
    monkeypatch.setattr(views, "send_coach_invite_email", lambda **kwargs: True)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:coach_invite_resend", kwargs={"token": invite.token})
        )

    assert response.status_code == 302
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachinvite")


def test_invite_claim_locks_use_no_key(client):
    athlete = UserFactory()
    declined = CoachInviteFactory()
    expired = CoachInviteFactory(expires_at=timezone.now())
    client.force_login(athlete)

    with CaptureQueriesContext(connection) as queries:
        declined_response = client.post(
            reverse("meso:invite_claim", kwargs={"token": declined.token}),
            {"action": "decline"},
        )
        expired_response = client.get(
            reverse("meso:invite_claim", kwargs={"token": expired.token})
        )

    assert declined_response.status_code == 302
    assert expired_response.status_code == 200
    _assert_no_key_self_locks(queries.captured_queries, "meso_coachinvite", count=2)


def test_session_add_mesocycle_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    plan = relationship.create_plan()
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:api_session_add", kwargs={"plan_id": plan.pk})
        )

    assert response.status_code == 201
    _assert_no_key_self_locks(queries.captured_queries, "meso_mesocycle")


def test_week_add_mesocycle_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    plan = relationship.create_plan()
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:api_week_add", kwargs={"plan_id": plan.pk})
        )

    assert response.status_code == 201
    _assert_no_key_self_locks(queries.captured_queries, "meso_mesocycle")


def test_batch_apply_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    plan = relationship.create_plan()
    batch = AgentProposalBatchFactory(
        plan=plan,
        coach=relationship.coach,
        mesocycle=plan.mesocycles.get(),
    )
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:api_batch_apply", kwargs={"batch_id": batch.pk})
        )

    assert response.status_code == 200
    _assert_no_key_self_locks(queries.captured_queries, "meso_agentproposalbatch")


def test_batch_dismiss_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    plan = relationship.create_plan()
    batch = AgentProposalBatchFactory(plan=plan, coach=relationship.coach)
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:api_batch_dismiss", kwargs={"batch_id": batch.pk})
        )

    assert response.status_code == 200
    _assert_no_key_self_locks(queries.captured_queries, "meso_agentproposalbatch")


def test_change_set_status_batch_lock_uses_no_key(client):
    relationship = CoachAthleteFactory()
    batch = AgentProposalBatchFactory(
        plan__relationship=relationship, coach=relationship.coach
    )
    change = ProposedChangeFactory(batch=batch)
    client.force_login(relationship.coach)

    with CaptureQueriesContext(connection) as queries:
        response = client.post(
            reverse("meso:api_change_status", kwargs={"pk": change.pk}),
            data=json.dumps({"status": ProposedChange.Status.REJECTED}),
            content_type="application/json",
        )

    assert response.status_code == 200
    _assert_no_key_self_locks(queries.captured_queries, "meso_agentproposalbatch")


def test_still_resolvable_batch_lock_uses_no_key():
    batch = AgentProposalBatchFactory(status=AgentProposalBatch.Status.PENDING)

    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        assert agent_service._still_resolvable(
            batch,
            AgentProposalBatch.Status.PENDING,
            discarding="a test result",
        )

    _assert_no_key_self_locks(queries.captured_queries, "meso_agentproposalbatch")


def test_webhook_mirror_subscription_locks_use_no_key():
    existing = CoachSubscriptionFactory()
    missing = UserFactory()

    with CaptureQueriesContext(connection) as queries, transaction.atomic():
        assert webhooks._lock_mirror(existing.coach).pk == existing.pk
        assert webhooks._lock_mirror(missing) is None

    _assert_no_key_self_locks(
        queries.captured_queries, "meso_coachsubscription", count=3
    )


def test_invoice_nudge_subscription_lock_uses_no_key():
    subscription = CoachSubscriptionFactory(
        status=CoachSubscription.Status.ACTIVE,
        stripe_subscription_id="sub_lock_strength",
    )

    with CaptureQueriesContext(connection) as queries:
        webhooks._nudge_status(
            {"subscription": subscription.stripe_subscription_id},
            from_statuses=(CoachSubscription.Status.ACTIVE,),
            to_status=CoachSubscription.Status.PAST_DUE,
        )

    _assert_no_key_self_locks(queries.captured_queries, "meso_coachsubscription")


def test_still_resolvable_allows_fk_commit_but_blocks_a_second_caller():
    batch = AgentProposalBatchFactory(status=AgentProposalBatch.Status.PENDING)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    insert_errors = []
    contender_errors = []

    def hold_batch_lock():
        try:
            with transaction.atomic():
                assert agent_service._still_resolvable(
                    batch,
                    AgentProposalBatch.Status.PENDING,
                    discarding="the holder result",
                )
                locked.set()
                assert release.wait(timeout=8)
        except Exception as exc:  # pragma: no cover - surfaced below
            holder_errors.append(exc)
        finally:
            connection.close()

    def insert_referencing_change():
        try:
            _set_short_lock_timeout()
            with transaction.atomic():
                ProposedChangeFactory(batch=batch)
        except Exception as exc:  # pragma: no cover - surfaced below
            insert_errors.append(exc)
        finally:
            connection.close()

    def contend_for_batch_lock():
        try:
            _set_short_lock_timeout()
            with transaction.atomic():
                agent_service._still_resolvable(
                    batch,
                    AgentProposalBatch.Status.PENDING,
                    discarding="the contender result",
                )
        except Exception as exc:  # pragma: no cover - asserted below
            contender_errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_batch_lock)
    inserter = threading.Thread(target=insert_referencing_change)
    contender = threading.Thread(target=contend_for_batch_lock)
    holder.start()
    assert locked.wait(timeout=5), "_still_resolvable never acquired its batch lock"
    inserter.start()
    inserter.join(timeout=3)
    contender.start()
    contender.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    inserter.join(timeout=5)
    contender.join(timeout=5)

    assert not holder.is_alive()
    assert not inserter.is_alive()
    assert not contender.is_alive()
    assert holder_errors == []
    assert insert_errors == [], (
        "a deferred ProposedChange.batch FK commit was blocked by the batch mutex: "
        f"{insert_errors}"
    )
    assert len(contender_errors) == 1 and isinstance(
        contender_errors[0], DatabaseError
    ), f"a second _still_resolvable caller did not block: {contender_errors}"


def test_start_trial_for_allows_fk_commit_but_blocks_a_second_caller(monkeypatch):
    coach = UserFactory()
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    insert_errors = []
    contender_errors = []
    real_start_trial = CoachSubscription.start_trial

    monkeypatch.setattr("store_project.analytics.track.track", lambda *a, **kw: None)

    def start_trial_then_wait(subscription):
        locked.set()
        assert release.wait(timeout=8)
        return real_start_trial(subscription)

    monkeypatch.setattr(CoachSubscription, "start_trial", start_trial_then_wait)

    def hold_trial_lock():
        try:
            CoachSubscription.start_trial_for(coach)
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

    def contend_for_trial_lock():
        try:
            _set_short_lock_timeout()
            CoachSubscription.start_trial_for(coach)
        except Exception as exc:  # pragma: no cover - asserted below
            contender_errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_trial_lock)
    inserter = threading.Thread(target=insert_referencing_profile)
    contender = threading.Thread(target=contend_for_trial_lock)
    holder.start()
    assert locked.wait(timeout=5), "start_trial_for never acquired its User lock"
    inserter.start()
    inserter.join(timeout=3)
    contender.start()
    contender.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    inserter.join(timeout=5)
    contender.join(timeout=5)

    assert not holder.is_alive()
    assert not inserter.is_alive()
    assert not contender.is_alive()
    assert holder_errors == []
    assert insert_errors == [], (
        "a deferred CoachProfile.user FK commit was blocked by the trial mutex: "
        f"{insert_errors}"
    )
    assert len(contender_errors) == 1 and isinstance(
        contender_errors[0], DatabaseError
    ), f"a second start_trial_for caller did not block: {contender_errors}"


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
