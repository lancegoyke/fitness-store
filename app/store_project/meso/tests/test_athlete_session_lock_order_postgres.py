"""PostgreSQL lock-order regressions for athlete session writers (#588)."""

import json
import threading
import time
from datetime import timedelta

import pytest
from django.db import connection
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from store_project.meso import history
from store_project.meso import settle
from store_project.meso import views
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import SessionSlot
from store_project.meso.tests.test_athlete_logging import seed

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


def _settleable_log(s):
    log = SessionLogFactory(
        session=s.session,
        athlete=s.athlete,
        status=SessionLog.Status.PENDING,
    )
    LoggedSetFactory(session_log=log, prescription=s.squat)
    old = timezone.now() - settle.quiet_period() - timedelta(hours=1)
    SessionLog.objects.filter(pk=log.pk).update(last_activity_at=old)
    return log, timezone.now() - settle.quiet_period()


def _hold_row(model, pk, locked, release, errors):
    try:
        with transaction.atomic():
            model.objects.select_for_update().get(pk=pk)
            locked.set()
            assert release.wait(timeout=8)
    except Exception as exc:  # pragma: no cover - surfaced below
        errors.append(exc)
    finally:
        connection.close()


def _hold_row_no_key(model, pk, locked, release, errors):
    try:
        with transaction.atomic():
            model.objects.select_for_update(no_key=True).get(pk=pk)
            locked.set()
            assert release.wait(timeout=8)
    except Exception as exc:  # pragma: no cover - surfaced below
        errors.append(exc)
    finally:
        connection.close()


def _run_upsert_while_row_is_held(model, pk, seeded, cell):
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    upsert_errors = []
    holder = threading.Thread(
        # NO KEY still conflicts with the accidental joined-row lock, but lets
        # the upsert's LoggedSet child commit check its deferred FK (#611).
        target=_hold_row_no_key,
        args=(model, pk, locked, release, holder_errors),
    )

    def run_upsert():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '750ms'")
            with transaction.atomic():
                views._upsert_parsed_set(
                    seeded.session,
                    seeded.athlete,
                    seeded.squat,
                    cell,
                    previous_text="",
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            upsert_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_upsert)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    worker.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)
    assert not holder.is_alive()
    assert not worker.is_alive()
    assert holder_errors == []
    assert upsert_errors == []


def test_upsert_parsed_set_locks_prescription_not_joined_exercise_slot():
    joined_seed = seed()
    joined_cell = Prescription.objects.create(
        exercise_slot=joined_seed.squat.exercise_slot,
        week=joined_seed.week,
        line=1,
        text="225 x 5",
        athlete_authored=True,
    )

    _run_upsert_while_row_is_held(
        ExerciseSlot,
        joined_seed.squat.exercise_slot_id,
        joined_seed,
        joined_cell,
    )

    assert LoggedSet.objects.filter(source_line=joined_cell).exists(), (
        "_upsert_parsed_set waited on the joined meso_exerciseslot row"
    )

    target_seed = seed()
    target_cell = Prescription.objects.create(
        exercise_slot=target_seed.squat.exercise_slot,
        week=target_seed.week,
        line=1,
        text="225 x 5",
        athlete_authored=True,
    )

    _run_upsert_while_row_is_held(
        Prescription,
        target_seed.squat.pk,
        target_seed,
        target_cell,
    )

    assert not LoggedSet.objects.filter(source_line=target_cell).exists(), (
        "_upsert_parsed_set did not lock its meso_prescription row"
    )


def test_settle_session_lock_does_not_lock_the_joined_session_slot():
    s = seed()
    log, cutoff = _settleable_log(s)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    settle_errors = []
    result = {}

    holder = threading.Thread(
        target=_hold_row,
        args=(SessionSlot, s.session.session_slot_id, locked, release, holder_errors),
    )

    def run_settle():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '750ms'")
            result["settled"] = settle.settle_log(log.pk, cutoff=cutoff)
        except Exception as exc:  # pragma: no cover - surfaced below
            settle_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_settle)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    worker.join(timeout=3)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)

    assert holder_errors == []
    assert settle_errors == [], (
        "settle_log's Session lock waited on the joined SessionSlot row: "
        f"{settle_errors}"
    )
    assert result == {"settled": True}


def test_athlete_log_session_waits_for_the_plan_lock():
    s = seed()
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    request_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_row,
        args=(Plan, s.plan.pk, locked, release, holder_errors),
    )

    def run_request():
        client = Client()
        client.force_login(s.athlete)
        try:
            response = client.post(
                reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
                data=json.dumps({"sets": []}),
                content_type="application/json",
            )
            result["status"] = response.status_code
        except Exception as exc:  # pragma: no cover - surfaced below
            request_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_request)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    request_was_blocked = _wait_until_a_backend_is_lock_blocked()
    release.set()
    holder.join(timeout=8)
    worker.join(timeout=8)

    assert request_was_blocked, "athlete_log_session did not wait on the Plan row"
    assert holder_errors == []
    assert request_errors == []
    assert result == {"status": 200}


def test_settle_log_waits_for_the_plan_lock():
    s = seed()
    log, cutoff = _settleable_log(s)
    locked = threading.Event()
    release = threading.Event()
    holder_errors = []
    settle_errors = []
    result = {}
    holder = threading.Thread(
        target=_hold_row,
        args=(Plan, s.plan.pk, locked, release, holder_errors),
    )

    def run_settle():
        try:
            result["settled"] = settle.settle_log(log.pk, cutoff=cutoff)
        except Exception as exc:  # pragma: no cover - surfaced below
            settle_errors.append(exc)
        finally:
            connection.close()

    worker = threading.Thread(target=run_settle)
    holder.start()
    assert locked.wait(timeout=5)
    worker.start()
    settle_was_blocked = _wait_until_a_backend_is_lock_blocked()
    release.set()
    holder.join(timeout=8)
    worker.join(timeout=8)

    assert settle_was_blocked, "settle_log did not wait on the Plan row"
    assert holder_errors == []
    assert settle_errors == []
    assert result == {"settled": True}


def test_restore_and_athlete_log_session_serialize_on_plan(monkeypatch):
    s = seed()
    with transaction.atomic():
        history.record_plan_action(s.plan, "Before athlete save")

    athlete_reached = threading.Event()
    athlete_go = threading.Event()
    restore_reached = threading.Event()
    restore_go = threading.Event()
    athlete_errors = []
    undo_errors = []
    statuses = {}
    real_newest = views.newest_session_logs
    real_restore = views.restore_plan_snapshot

    def newest_then_wait(*args, **kwargs):
        athlete_reached.set()
        assert athlete_go.wait(timeout=8)
        return real_newest(*args, **kwargs)

    def restore_then_wait(*args, **kwargs):
        restore_reached.set()
        assert restore_go.wait(timeout=8)
        return real_restore(*args, **kwargs)

    monkeypatch.setattr(views, "newest_session_logs", newest_then_wait)
    monkeypatch.setattr(views, "restore_plan_snapshot", restore_then_wait)

    def run_athlete():
        client = Client()
        client.force_login(s.athlete)
        try:
            response = client.post(
                reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
                data=json.dumps({"sets": []}),
                content_type="application/json",
            )
            statuses["athlete"] = response.status_code
        except Exception as exc:  # pragma: no cover - surfaced below
            athlete_errors.append(exc)
        finally:
            connection.close()

    def run_undo():
        client = Client()
        client.force_login(s.coach)
        try:
            response = client.post(
                reverse("meso:api_plan_undo", kwargs={"plan_id": s.plan.pk})
            )
            statuses["undo"] = response.status_code
        except Exception as exc:  # pragma: no cover - surfaced below
            undo_errors.append(exc)
        finally:
            connection.close()

    athlete = threading.Thread(target=run_athlete)
    undo = threading.Thread(target=run_undo)
    athlete.start()
    assert athlete_reached.wait(timeout=5)
    undo.start()
    restore_reached_promptly = restore_reached.wait(timeout=1)
    athlete_go.set()
    restore_go.set()
    athlete.join(timeout=10)
    undo.join(timeout=10)

    assert not restore_reached_promptly, (
        "undo reached restore while athlete_log_session held Session; the athlete "
        "path did not acquire Plan first"
    )
    assert not athlete.is_alive()
    assert not undo.is_alive()
    assert athlete_errors == []
    assert undo_errors == []
    assert statuses == {"athlete": 200, "undo": 200}


def test_athlete_log_session_returns_404_if_locked_session_vanished(monkeypatch):
    s = seed()
    client = Client()
    client.force_login(s.athlete)

    class MissingLockedSession:
        def filter(self, **kwargs):
            return self

        def first(self):
            return None

    monkeypatch.setattr(
        Session.objects,
        "select_for_update",
        lambda *args, **kwargs: MissingLockedSession(),
    )

    response = client.post(
        reverse("meso:athlete_log_session", kwargs={"pk": s.session.pk}),
        data=json.dumps({"sets": []}),
        content_type="application/json",
    )

    assert response.status_code == 404
