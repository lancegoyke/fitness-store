"""PostgreSQL race: a coach decline cannot overwrite a concurrent activation (#679)."""

import threading
import time

import pytest
from django.db import connection
from django.db import transaction

from store_project.meso.billing.activation import activate_waiting
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import InvalidTransition
from store_project.users.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Row locks are not observable on SQLite.",
    ),
]

WAITING = CoachAthlete.Status.ACCEPTED_WAITING


def _wait_until_a_backend_is_lock_blocked(timeout=3.0):
    deadline = time.monotonic() + timeout
    with connection.cursor() as cursor:
        while time.monotonic() < deadline:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
            if cursor.fetchone()[0]:
                return True
            time.sleep(0.02)
    return False


def test_decline_waits_for_an_in_flight_activation_and_then_refuses():
    coach = UserFactory()
    link = CoachAthleteFactory(coach=coach, athlete=UserFactory(), status=WAITING)
    stale = CoachAthlete.objects.get(pk=link.pk)  # still says "waiting"
    activated = threading.Event()
    release = threading.Event()
    outcome = {}

    def activate():
        try:
            with transaction.atomic():
                activate_waiting(coach.pk)
                activated.set()
                assert release.wait(timeout=10)
        finally:
            connection.close()

    def decline():
        try:
            stale.decline()
            outcome["decline"] = "declined"
        except InvalidTransition:
            outcome["decline"] = "refused"
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["decline"] = repr(exc)
        finally:
            connection.close()

    activator = threading.Thread(target=activate)
    decliner = threading.Thread(target=decline)
    activator.start()
    assert activated.wait(timeout=5)
    decliner.start()
    blocked = _wait_until_a_backend_is_lock_blocked()
    release.set()
    activator.join(timeout=10)
    decliner.join(timeout=10)

    assert blocked, "decline did not wait on the link row lock"
    assert outcome["decline"] == "refused"
    link.refresh_from_db()
    assert link.status == CoachAthlete.Status.ACTIVE
