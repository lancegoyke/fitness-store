"""N4 — invite lifecycle (Phase 4): the expiry-reminder email.

Phase 3 gave a ``CoachInvite`` a TTL but no nudge — a claim link silently nears
expiry and the athlete only finds out it's dead when they click. Phase 4 sends a
reminder before the link lapses:

- ``reminder_sent_at`` records that a reminder went out (so a sweep won't
  re-nudge the same arming cycle);
- ``INVITE_REMINDER_LEAD`` defines the window — a pending invite is *due for a
  reminder* once it's within the lead of ``expires_at`` but not yet past due;
- ``needs_reminder`` (single-invite) / ``due_for_reminder()`` (queryset) derive
  that window; a null-clock legacy invite never expires, so never needs one;
- ``mark_reminded()`` stamps the timestamp; ``resend()`` clears it (a re-armed
  invite re-earns a reminder near its new expiry);
- ``send_coach_invite_reminder_email`` is the notifications channel;
- the ``meso_remind_expiring_invites`` management command sweeps the due set,
  emails each best-effort, and stamps them — the reminder peer of
  ``meso_expire_invites``.

See ``docs/meso/invites-plan.md``. Mail is best-effort; the command sends
synchronously (no request, no ``on_commit``), so send-asserting tests read
``mail.outbox`` directly.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from store_project.meso.models import CoachInvite
from store_project.notifications.emails import send_coach_invite_reminder_email
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _expires_in(invite, *, days):
    """Set an invite's clock ``days`` from now (negative = already past due)."""
    invite.expires_at = timezone.now() + timedelta(days=days)
    invite.save(update_fields=["expires_at"])
    return invite


# -- model: the reminder window -------------------------------------------


class TestReminderWindow:
    def test_needs_reminder_within_lead(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=1)  # inside the 3-day lead
        assert invite.needs_reminder is True

    def test_no_reminder_far_from_expiry(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        # open_for stamps now + 14d, well outside the lead
        assert invite.needs_reminder is False

    def test_no_reminder_for_null_clock(self):
        """A legacy invite with no TTL never expires, so never needs a reminder."""
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        invite.expires_at = None
        invite.save(update_fields=["expires_at"])
        assert invite.needs_reminder is False

    def test_no_reminder_once_past_due(self):
        """Past expiry is expire()/resend() territory, not a reminder."""
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=-1)
        assert invite.needs_reminder is False

    def test_no_reminder_when_already_reminded(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=1)
        invite.mark_reminded()
        assert invite.needs_reminder is False

    def test_no_reminder_when_not_pending(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=1)
        invite.revoke()
        assert invite.needs_reminder is False


class TestDueForReminderQueryset:
    def test_returns_only_in_window_unreminded(self):
        coach = UserFactory()
        due, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(due, days=2)
        far, _ = CoachInvite.open_for(coach=coach, email="far@example.com")  # +14d
        overdue, _ = CoachInvite.open_for(coach=coach, email="overdue@example.com")
        _expires_in(overdue, days=-1)
        reminded, _ = CoachInvite.open_for(coach=coach, email="done@example.com")
        _expires_in(reminded, days=2)
        reminded.mark_reminded()
        nullclock, _ = CoachInvite.open_for(coach=coach, email="legacy@example.com")
        nullclock.expires_at = None
        nullclock.save(update_fields=["expires_at"])

        assert set(CoachInvite.objects.due_for_reminder()) == {due}

    def test_excludes_answered_invites_in_window(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="gone@example.com")
        _expires_in(invite, days=1)
        invite.revoke()
        assert invite not in CoachInvite.objects.due_for_reminder()


# -- model: mark_reminded --------------------------------------------------


class TestMarkReminded:
    def test_stamps_and_persists(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        assert invite.reminder_sent_at is None
        invite.mark_reminded()
        invite.refresh_from_db()
        assert invite.reminder_sent_at is not None

    def test_drops_out_of_due_set(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=1)
        assert invite in CoachInvite.objects.due_for_reminder()
        invite.mark_reminded()
        assert invite not in CoachInvite.objects.due_for_reminder()


# -- model: resend re-arms the reminder ------------------------------------


class TestResendClearsReminder:
    def test_resend_clears_reminder_flag(self):
        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="ath@example.com")
        _expires_in(invite, days=1)
        invite.mark_reminded()
        invite.resend()
        assert invite.reminder_sent_at is None
        invite.refresh_from_db()
        assert invite.reminder_sent_at is None


# -- notifications: the reminder email -------------------------------------


class TestReminderEmail:
    def test_sends_with_claim_link(self):
        coach = UserFactory(name="Coach Carter")
        url = "https://example.com/meso/claim/abc/"
        sent = send_coach_invite_reminder_email(
            coach=coach, email="ath@example.com", accept_url=url
        )
        assert sent is True
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        assert msg.to == ["ath@example.com"]
        assert "Coach Carter" in msg.subject + msg.body
        assert url in msg.body

    def test_skips_without_email(self):
        coach = UserFactory()
        sent = send_coach_invite_reminder_email(
            coach=coach, email="", accept_url="https://example.com/x/"
        )
        assert sent is False
        assert mail.outbox == []


# -- command: meso_remind_expiring_invites ---------------------------------


class TestRemindCommand:
    def test_emails_and_stamps_due_invites(self):
        coach = UserFactory()
        due, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(due, days=1)
        call_command("meso_remind_expiring_invites")
        due.refresh_from_db()
        assert due.reminder_sent_at is not None
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["due@example.com"]
        assert str(due.token) in mail.outbox[0].body

    def test_skips_far_and_nullclock_and_overdue(self):
        coach = UserFactory()
        far, _ = CoachInvite.open_for(coach=coach, email="far@example.com")  # +14d
        overdue, _ = CoachInvite.open_for(coach=coach, email="overdue@example.com")
        _expires_in(overdue, days=-1)
        legacy, _ = CoachInvite.open_for(coach=coach, email="legacy@example.com")
        legacy.expires_at = None
        legacy.save(update_fields=["expires_at"])
        call_command("meso_remind_expiring_invites")
        assert mail.outbox == []
        for inv in (far, overdue, legacy):
            inv.refresh_from_db()
            assert inv.reminder_sent_at is None

    def test_dry_run_changes_nothing(self):
        coach = UserFactory()
        due, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(due, days=1)
        call_command("meso_remind_expiring_invites", "--dry-run")
        due.refresh_from_db()
        assert due.reminder_sent_at is None
        assert mail.outbox == []

    def test_idempotent_second_run_is_quiet(self):
        coach = UserFactory()
        due, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(due, days=1)
        call_command("meso_remind_expiring_invites")
        mail.outbox.clear()
        call_command("meso_remind_expiring_invites")
        assert mail.outbox == []

    def test_skips_invite_claimed_concurrently(self):
        """The re-check under the row lock drops a row another worker just stamped.

        Simulates an overlapping sweep: a row is in this run's due-list, but a
        concurrent worker stamps ``reminder_sent_at`` before this run acquires the
        lock — so no duplicate reminder goes out.
        """
        from django.contrib.sites.models import Site

        from store_project.meso.management.commands.meso_remind_expiring_invites import (  # noqa: E501
            Command,
        )

        coach = UserFactory()
        invite, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(invite, days=1)
        stale = CoachInvite.objects.get(pk=invite.pk)  # the row the loop holds
        # a concurrent worker claims + stamps it first
        CoachInvite.objects.get(pk=invite.pk).mark_reminded()
        sent = Command()._remind(stale, site=Site.objects.get_current(), scheme="https")
        assert sent is False
        assert mail.outbox == []

    def test_mail_failure_does_not_stamp(self):
        """A bounced reminder is logged and left un-stamped so the next sweep retries."""
        coach = UserFactory()
        due, _ = CoachInvite.open_for(coach=coach, email="due@example.com")
        _expires_in(due, days=1)
        with mock.patch(
            "store_project.meso.management.commands."
            "meso_remind_expiring_invites.send_coach_invite_reminder_email",
            side_effect=RuntimeError("smtp down"),
        ):
            call_command("meso_remind_expiring_invites")  # no traceback escapes
        due.refresh_from_db()
        assert due.reminder_sent_at is None
