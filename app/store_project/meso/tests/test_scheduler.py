"""App-managed scheduling for the N4 invite sweeps (django-q2).

Rather than a hand-rolled box cron, the two invite sweeps run as
``django_q.Schedule`` rows registered by migration
``0018_register_invite_schedules`` and executed by the ``qcluster`` worker. This
covers the two halves that live in *our* code:

- ``store_project.meso.tasks`` — the stable wrappers the schedules point at, each
  driving its management command (so the sweep logic has one home);
- the registered schedules themselves — present, daily, and pointing at an
  importable callable (a rename of ``tasks`` that didn't update the migration
  would break the scheduler silently in prod; this catches it).

The cluster process and the ORM broker aren't exercised here (no cluster spins up
under ``Q_CLUSTER["sync"]``); we test the unit of work and its registration.
"""

import importlib
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from django_q.models import Schedule

from store_project.meso import tasks
from store_project.meso.models import CoachInvite
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


# -- task wrappers run the sweeps ------------------------------------------


class TestInviteTasks:
    def test_expire_invites_task_expires_overdue(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.expires_at = timezone.now() - timedelta(days=1)
        invite.save(update_fields=["expires_at"])
        tasks.expire_invites()
        invite.refresh_from_db()
        assert invite.status == CoachInvite.Status.EXPIRED

    def test_remind_task_sends_and_stamps(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.expires_at = timezone.now() + timedelta(days=1)  # inside the lead
        invite.save(update_fields=["expires_at"])
        tasks.remind_expiring_invites()
        invite.refresh_from_db()
        assert invite.reminder_sent_at is not None
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ath@example.com"]


# -- the registered schedules ----------------------------------------------


class TestScheduleRegistration:
    EXPECTED = {
        "meso-expire-invites": "store_project.meso.tasks.expire_invites",
        "meso-remind-expiring-invites": "store_project.meso.tasks.remind_expiring_invites",
        "meso-reconcile-seats": "store_project.meso.tasks.reconcile_seats",
    }

    def test_invite_schedules_registered_daily(self):
        for name, func in self.EXPECTED.items():
            sched = Schedule.objects.get(name=name)
            assert sched.func == func
            assert sched.schedule_type == Schedule.DAILY

    def test_schedule_funcs_are_importable_callables(self):
        """Every registered func path resolves to a callable (catches renames)."""
        for func in self.EXPECTED.values():
            module_path, _, attr = func.rpartition(".")
            resolved = getattr(importlib.import_module(module_path), attr)
            assert callable(resolved)
