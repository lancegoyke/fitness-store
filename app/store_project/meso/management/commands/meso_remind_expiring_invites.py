"""Email reminders for pending coach invites nearing expiry (N4 Phase 4).

A pending ``CoachInvite`` carries a TTL (``expires_at``) but no nudge — a claim
link silently nears expiry and the athlete only learns it's dead when they
finally click. This sweep emails a reminder once an invite enters its reminder
window (``due_for_reminder`` — within ``INVITE_REMINDER_LEAD`` of expiry, not yet
past due, not yet reminded) and stamps ``reminder_sent_at`` so a later run skips
it. The reminder peer of ``meso_expire_invites``; safe to run on a cron.

Each email is best-effort: a mail-backend failure is logged and the invite is
left un-stamped so the next sweep retries it. The absolute claim URL is built
off-request from the current ``Site``.

    manage.py meso_remind_expiring_invites
    manage.py meso_remind_expiring_invites --dry-run   # report the count, send nothing
"""

import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db import transaction
from django.urls import reverse

from store_project.meso.models import CoachInvite
from store_project.notifications.emails import send_coach_invite_reminder_email

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Email reminders for pending coach invites nearing expiry."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many reminders would send without sending anything.",
        )

    def handle(self, *args, **options):
        due = list(CoachInvite.objects.due_for_reminder())
        if options["dry_run"]:
            self.stdout.write(
                f"{len(due)} invite(s) due for a reminder (dry run — no changes)."
            )
            return

        site = Site.objects.get_current()
        scheme = "https" if getattr(settings, "SECURE_SSL_REDIRECT", False) else "http"
        sent = 0
        for invite in due:
            if self._remind(invite, site=site, scheme=scheme):
                sent += 1
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} invite reminder(s)."))

    def _remind(self, invite, *, site, scheme):
        """Claim one invite under a row lock and send its reminder; return sent?

        Re-fetching the row with ``select_for_update(skip_locked=True)`` and
        re-checking ``due_for_reminder`` under the lock makes the sweep safe to
        overlap: a concurrent run (or an operator rerun while a slow backend is
        still sending) skips a row another worker has claimed, so no athlete gets
        a duplicate. Send and ``mark_reminded`` share the transaction, so a mail
        failure rolls the stamp back and the next sweep retries the invite.
        """
        try:
            with transaction.atomic():
                locked = (
                    CoachInvite.objects.select_for_update(skip_locked=True)
                    .due_for_reminder()
                    .filter(pk=invite.pk)
                    .first()
                )
                if locked is None:
                    return False  # claimed by another worker, or no longer due
                accept_url = "{scheme}://{domain}{path}".format(
                    scheme=scheme,
                    domain=site.domain,
                    path=reverse("meso:invite_claim", kwargs={"token": locked.token}),
                )
                send_coach_invite_reminder_email(
                    coach=locked.coach, email=locked.email, accept_url=accept_url
                )
                locked.mark_reminded()
                return True
        except Exception:  # best-effort; rollback leaves it un-stamped to retry
            logger.exception("Failed to send invite reminder to %s", invite.email)
            return False
