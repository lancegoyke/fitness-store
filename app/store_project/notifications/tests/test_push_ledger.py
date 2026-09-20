"""The push ledger's writes from the send path (#509 slice 3).

``notifications.push.log_push_sent``/``log_push_error``/``url_with_notification``
are only ever called from ``meso.push._fan_out``, so these tests drive them
through the real send path — ``meso.push.notify_block_delivered``, the one
kind sent today — the same integration-style approach
``meso.tests.test_push.TestNotifyBlockDelivered`` uses for the transport
itself. That suite is unaffected: it never calls ``_fan_out`` directly, only
through the public ``notify_block_delivered``, so the ledger writes it now
does as a side effect ride along for free without a mocked notifications
layer.

The network send (``pywebpush.webpush``) is always mocked — these tests are
about the ledger, never about the encryption.
"""

import json
from unittest import mock
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from django.test import override_settings
from pywebpush import WebPushException

from store_project.meso import push as meso_push
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import PushSubscription
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.notifications.models import PushKind
from store_project.notifications.models import PushNotification

pytestmark = pytest.mark.django_db

PUSH_PATH = "store_project.meso.push.webpush"
DISABLED = dict(MESO_VAPID_PUBLIC_KEY="", MESO_VAPID_PRIVATE_KEY="")


def make_sub(athlete, endpoint="https://push.example.com/abc", p256dh="pk", auth="ak"):
    return PushSubscription.objects.create(
        athlete=athlete, endpoint=endpoint, p256dh=p256dh, auth=auth
    )


def gone_exception(status):
    return WebPushException("gone", response=mock.Mock(status_code=status))


def seed_plan():
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory()
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1)
    session = day(week, day_number=1, name="Lower")
    presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, week


def deliver(plan, mesocycle, week_count=1):
    return meso_push.notify_block_delivered(
        athlete=plan.athlete,
        coach=plan.coach,
        plan=plan,
        mesocycle=mesocycle,
        week_count=week_count,
        home_url="http://testserver/meso/me/",
    )


class TestSendWritesOneRowPerSubscription:
    def test_one_row_per_subscription_with_kind_and_user(self):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/1")
        make_sub(plan.athlete, endpoint="https://push/2")

        with mock.patch(PUSH_PATH):
            sent = deliver(plan, week.mesocycle, week_count=2)

        assert sent == 2
        rows = list(PushNotification.objects.all())
        assert len(rows) == 2
        for row in rows:
            assert row.kind == PushKind.BLOCK_DELIVERED
            assert row.user == plan.athlete
            assert row.error == ""

    def test_each_devices_payload_url_carries_its_own_rows_id(self):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/1")
        make_sub(plan.athlete, endpoint="https://push/2")

        with mock.patch(PUSH_PATH) as webpush:
            deliver(plan, week.mesocycle, week_count=2)

        row_ids = {str(row.pk) for row in PushNotification.objects.all()}
        seen_ids = set()
        for call in webpush.call_args_list:
            payload = json.loads(call.kwargs["data"])
            parts = urlsplit(payload["url"])
            assert parts.path == "/meso/me/"
            qs = parse_qs(parts.query)
            assert list(qs.keys()) == ["n"]
            seen_ids.add(qs["n"][0])
        # Two devices, two distinct ids — never the shared payload's id twice.
        assert len(seen_ids) == 2
        assert seen_ids == row_ids


class TestRejectedEndpointRecordsError:
    def test_410_records_the_error_and_still_prunes(self):
        plan, week = seed_plan()
        sub = make_sub(plan.athlete, endpoint="https://push/dead")

        with mock.patch(PUSH_PATH, side_effect=gone_exception(410)):
            sent = deliver(plan, week.mesocycle)

        assert sent == 0
        row = PushNotification.objects.get()
        assert row.error != ""
        assert "410" in row.error
        # The dead endpoint is still pruned exactly as before the ledger existed.
        assert not PushSubscription.objects.filter(pk=sub.pk).exists()

    def test_a_non_gone_failure_also_records_the_error(self):
        plan, week = seed_plan()
        sub = make_sub(plan.athlete, endpoint="https://push/flaky")

        with mock.patch(PUSH_PATH, side_effect=gone_exception(500)):
            sent = deliver(plan, week.mesocycle)

        assert sent == 0
        row = PushNotification.objects.get()
        assert row.error != ""
        assert "500" in row.error
        # A transient failure isn't "gone" — the subscription survives.
        assert PushSubscription.objects.filter(pk=sub.pk).exists()


class TestLedgerFailureNeverStopsTheSend:
    def test_the_model_create_raising_does_not_stop_the_push(self):
        # Patching log_push_sent itself would only prove our own try/except
        # works; patching the model's own write proves the send survives a
        # *real* DB failure at the exact call log_push_sent makes.
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/1")

        with mock.patch(
            "store_project.notifications.models.PushNotification.objects.create",
            side_effect=RuntimeError("db is down"),
        ):
            with mock.patch(PUSH_PATH) as webpush:
                sent = deliver(plan, week.mesocycle)

        assert sent == 1
        webpush.assert_called_once()
        assert PushNotification.objects.count() == 0


class TestPushDisabledWritesNoRows:
    @override_settings(**DISABLED)
    def test_disabled_writes_no_rows(self):
        plan, week = seed_plan()
        make_sub(plan.athlete, endpoint="https://push/1")

        with mock.patch(PUSH_PATH) as webpush:
            sent = deliver(plan, week.mesocycle)

        assert sent == 0
        webpush.assert_not_called()
        assert PushNotification.objects.count() == 0
