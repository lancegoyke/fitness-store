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
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone
from django_q.models import Schedule

from store_project.meso import tasks
from store_project.meso.billing import agent_usage_report as report_mod
from store_project.meso.factories import AgentProposalBatchFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachInvite
from store_project.meso.models import CoachSubscription
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


# -- the margin-alert task runs over the previous month --------------------


class TestMarginAlertTask:
    def test_margin_alert_task_emails_owner_for_last_month_risk(self):
        """The wrapper reports the *previous* month and alerts the owner."""
        prev_start, _ = report_mod.previous_month_bounds()
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.ACTIVE)
        coach = sub.coach
        plan = PlanFactory(relationship__coach=coach)
        batch = AgentProposalBatchFactory(
            plan=plan,
            coach=coach,
            billing_status=CoachSubscription.Status.ACTIVE,
            estimated_cost_usd=Decimal("12.00"),  # > 50% of the flat $19 revenue
        )
        AgentProposalBatch.objects.filter(pk=batch.pk).update(
            created_at=prev_start + timedelta(days=1)
        )

        tasks.agent_margin_alert()

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["lance@lancegoyke.com"]
        assert coach.display_name() in mail.outbox[0].body


# -- the registered schedules ----------------------------------------------


class TestScheduleRegistration:
    EXPECTED = {
        "meso-expire-invites": "store_project.meso.tasks.expire_invites",
        "meso-remind-expiring-invites": "store_project.meso.tasks.remind_expiring_invites",
    }

    def test_invite_schedules_registered_daily(self):
        for name, func in self.EXPECTED.items():
            sched = Schedule.objects.get(name=name)
            assert sched.func == func
            assert sched.schedule_type == Schedule.DAILY

    def test_reconcile_seats_schedule_dropped(self):
        """The flat plan (D14) removed per-seat billing → the sweep is gone (0028)."""
        assert not Schedule.objects.filter(name="meso-reconcile-seats").exists()

    def test_schedule_funcs_are_importable_callables(self):
        """Every registered func path resolves to a callable (catches renames)."""
        for func in self.EXPECTED.values():
            module_path, _, attr = func.rpartition(".")
            resolved = getattr(importlib.import_module(module_path), attr)
            assert callable(resolved)


class TestMarginAlertScheduleRegistration:
    NAME = "meso-agent-margin-alert"
    FUNC = "store_project.meso.tasks.agent_margin_alert"

    def test_margin_alert_schedule_registered_monthly(self):
        sched = Schedule.objects.get(name=self.NAME)
        assert sched.func == self.FUNC
        assert sched.schedule_type == Schedule.MONTHLY

    def test_margin_alert_schedule_anchored_to_month_boundary(self):
        # Anchored to the 1st of a month in the future, so it doesn't fire on
        # deploy (a surprise owner email) or lag a month behind the deploy day.
        sched = Schedule.objects.get(name=self.NAME)
        assert sched.next_run is not None
        assert timezone.localtime(sched.next_run).day == 1  # local month boundary
        assert sched.next_run > timezone.now()

    def test_margin_alert_func_is_importable_callable(self):
        module_path, _, attr = self.FUNC.rpartition(".")
        resolved = getattr(importlib.import_module(module_path), attr)
        assert callable(resolved)


class TestSandboxExpiryScheduleRegistration:
    """The hourly sandbox-expiry sweep is registered (issue #389, Phase 2)."""

    NAME = "meso-expire-sandboxes"
    FUNC = "store_project.meso.tasks.expire_sandboxes"

    def test_sandbox_expiry_schedule_registered_hourly(self):
        sched = Schedule.objects.get(name=self.NAME)
        assert sched.func == self.FUNC
        assert sched.schedule_type == Schedule.HOURLY

    def test_sandbox_expiry_func_is_importable_callable(self):
        module_path, _, attr = self.FUNC.rpartition(".")
        resolved = getattr(importlib.import_module(module_path), attr)
        assert callable(resolved)
