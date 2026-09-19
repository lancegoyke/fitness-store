"""RED tests for #509 slice 2 — 13-month ``Event`` retention.

Read first: `docs/meso/decisions.md` "First-party usage events (#509)";
`meso/migrations/0047_register_settle_schedule.py`; `meso/tests/test_scheduler.py`.

Pre-implementation this is RED: `store_project.analytics.retention` doesn't
exist yet (`ImportError`, raised inside each test that needs it rather than
at module scope), the `analytics_purge_events` management command doesn't
exist yet (`call_command` raises a `CommandError` for the unknown command),
`store_project.analytics.tasks` doesn't exist yet (`ImportError`), and the
registration migration doesn't exist yet, so the `Schedule` row is simply
missing (`Schedule.DoesNotExist`).
"""

import datetime
import importlib
from io import StringIO

import pytest
from dateutil.relativedelta import relativedelta
from django.core.management import call_command
from django.utils import timezone
from django_q.models import Schedule

from store_project.analytics.events import EventName
from store_project.analytics.models import Event

pytestmark = pytest.mark.django_db

# Mirrors the retention window the not-yet-written `retention.py` will define
# (`RETENTION = relativedelta(months=13)`) — kept independent here so these
# tests don't depend on importing the module under test for their own math.
RETENTION = relativedelta(months=13)


def _event(created):
    return Event.objects.create(name=EventName.PLAN_CREATED, created=created)


class TestPurgeExpiredEvents:
    def test_rows_older_than_13_months_are_deleted(self):
        from store_project.analytics.retention import purge_expired_events

        now = timezone.now()
        old = _event(now - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_events(now=now)

        assert deleted == 1
        assert not Event.objects.filter(pk=old.pk).exists()

    def test_a_row_just_inside_the_window_is_kept(self):
        from store_project.analytics.retention import purge_expired_events

        now = timezone.now()
        kept = _event(now - RETENTION + datetime.timedelta(days=1))

        deleted = purge_expired_events(now=now)

        assert deleted == 0
        assert Event.objects.filter(pk=kept.pk).exists()

    def test_returns_the_number_deleted(self):
        from store_project.analytics.retention import purge_expired_events

        now = timezone.now()
        for _ in range(3):
            _event(now - RETENTION - datetime.timedelta(days=1))
        _event(now)  # kept, must not be counted

        deleted = purge_expired_events(now=now)

        assert deleted == 3

    def test_batching_deletes_all_expired_rows_across_multiple_batches(self):
        from store_project.analytics.retention import purge_expired_events

        now = timezone.now()
        for _ in range(5):
            _event(now - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_events(now=now, batch_size=2)

        assert deleted == 5
        assert Event.objects.count() == 0

    def test_now_defaults_to_the_current_time(self):
        from store_project.analytics.retention import purge_expired_events

        old = _event(timezone.now() - RETENTION - datetime.timedelta(days=1))

        deleted = purge_expired_events()

        assert deleted == 1
        assert not Event.objects.filter(pk=old.pk).exists()


class TestPurgeCommand:
    def test_command_deletes_expired_rows_and_keeps_the_rest(self):
        now = timezone.now()
        _event(now - RETENTION - datetime.timedelta(days=1))
        kept = _event(now)

        call_command("analytics_purge_events")

        assert Event.objects.count() == 1
        assert Event.objects.filter(pk=kept.pk).exists()

    def test_command_writes_the_deleted_count(self):
        now = timezone.now()
        _event(now - RETENTION - datetime.timedelta(days=1))

        out = StringIO()
        call_command("analytics_purge_events", stdout=out)

        assert "1" in out.getvalue()


class TestPurgeTask:
    def test_task_runs_the_purge_via_the_command(self):
        from store_project.analytics import tasks

        now = timezone.now()
        _event(now - RETENTION - datetime.timedelta(days=1))
        kept = _event(now)

        tasks.purge_expired_events()

        assert Event.objects.count() == 1
        assert Event.objects.filter(pk=kept.pk).exists()


class TestScheduleRegistration:
    """Mirrors ``meso.tests.test_scheduler.TestSettleScheduleRegistration``."""

    NAME = "analytics-purge-expired-events"
    FUNC = "store_project.analytics.tasks.purge_expired_events"

    def test_schedule_registered_daily(self):
        sched = Schedule.objects.get(name=self.NAME)
        assert sched.func == self.FUNC
        assert sched.schedule_type == Schedule.DAILY

    def test_schedule_func_is_an_importable_callable(self):
        module_path, _, attr = self.FUNC.rpartition(".")
        resolved = getattr(importlib.import_module(module_path), attr)
        assert callable(resolved)
