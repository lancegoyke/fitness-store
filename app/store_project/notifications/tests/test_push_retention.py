"""13-month retention sweep for the push ledger (#509 slice 3).

Mirrors ``analytics.tests.test_retention.TestPurgeExpiredEvents`` /
``TestPurgeCommand``: same window, same batched-loop shape, just over
``PushNotification.sent_at`` instead of ``Event.created``. The command test
here also covers ``analytics_purge_events`` reporting both ledgers' counts in
one run — the actual ``Event`` half of that command is already pinned by
``analytics.tests.test_retention.TestPurgeCommand``, untouched by this slice.
"""

import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.analytics.retention import RETENTION
from store_project.notifications.models import PushKind
from store_project.notifications.models import PushNotification
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _push(sent_at, user=None):
    row = PushNotification.objects.create(kind=PushKind.BLOCK_DELIVERED, user=user)
    # sent_at has an auto `default=timezone.now`; backdate it with a direct
    # update so the retention window can be exercised deterministically.
    PushNotification.objects.filter(pk=row.pk).update(sent_at=sent_at)
    row.refresh_from_db()
    return row


class TestPurgeExpiredPushNotifications:
    def test_a_row_older_than_13_months_is_purged(self):
        from store_project.notifications.retention import (
            purge_expired_push_notifications,
        )

        now = timezone.now()
        old = _push(now - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_push_notifications(now=now)

        assert deleted == 1
        assert not PushNotification.objects.filter(pk=old.pk).exists()

    def test_a_row_inside_the_window_survives(self):
        from store_project.notifications.retention import (
            purge_expired_push_notifications,
        )

        now = timezone.now()
        kept = _push(now - RETENTION + datetime.timedelta(days=1))

        deleted = purge_expired_push_notifications(now=now)

        assert deleted == 0
        assert PushNotification.objects.filter(pk=kept.pk).exists()

    def test_batching_deletes_all_expired_rows_across_multiple_batches(self):
        from store_project.notifications.retention import (
            purge_expired_push_notifications,
        )

        now = timezone.now()
        for _ in range(5):
            _push(now - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_push_notifications(now=now, batch_size=2)

        assert deleted == 5
        assert PushNotification.objects.count() == 0

    def test_now_defaults_to_the_current_time(self):
        from store_project.notifications.retention import (
            purge_expired_push_notifications,
        )

        old = _push(timezone.now() - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_push_notifications()

        assert deleted == 1
        assert not PushNotification.objects.filter(pk=old.pk).exists()


class TestPurgeCommandReportsBothLedgers:
    def test_the_command_purges_and_reports_both_counts(self):
        now = timezone.now()
        expired_event = Event.objects.create(name=EventName.PLAN_CREATED)
        Event.objects.filter(pk=expired_event.pk).update(
            created=now - RETENTION - datetime.timedelta(days=1)
        )
        kept_event = Event.objects.create(name=EventName.PLAN_CREATED)

        expired_push = _push(now - RETENTION - datetime.timedelta(days=1))
        kept_push = _push(now)

        out = StringIO()
        call_command("analytics_purge_events", stdout=out)

        assert not Event.objects.filter(pk=expired_event.pk).exists()
        assert Event.objects.filter(pk=kept_event.pk).exists()
        assert not PushNotification.objects.filter(pk=expired_push.pk).exists()
        assert PushNotification.objects.filter(pk=kept_push.pk).exists()

        output = out.getvalue()
        assert "1 expired event" in output
        assert "1 expired push notification" in output

    def test_a_user_actor_is_accepted_without_error(self):
        # Sanity: the ledger rows the command sweeps can carry a real user,
        # same as production traffic would.
        user = UserFactory()
        _push(timezone.now() - RETENTION - datetime.timedelta(days=1), user=user)

        call_command("analytics_purge_events", stdout=StringIO())

        assert PushNotification.objects.count() == 0
