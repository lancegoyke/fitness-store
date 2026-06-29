"""Sweep overdue coach invites past their TTL → ``expired`` (N4 Phase 3).

A pending ``CoachInvite`` becomes unclaimable the moment its ``expires_at``
passes (``is_claimable`` / the claim view enforce that lazily), but its *status*
stays ``pending`` until something flips it. This command does the bulk flip so
the roster reads them as expired (with a Resend action) and they drop out of the
claimable set — without waiting for someone to visit each stale link.

Idempotent and side-effect-free beyond the status flip; safe to run on a cron.

    manage.py meso_expire_invites
    manage.py meso_expire_invites --dry-run    # report the count, change nothing
"""

from django.core.management.base import BaseCommand

from store_project.meso.models import CoachInvite


class Command(BaseCommand):
    help = "Mark overdue pending coach invites as expired."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many invites would expire without changing anything.",
        )

    def handle(self, *args, **options):
        overdue = CoachInvite.objects.overdue()
        count = overdue.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} invite(s) overdue (dry run — no changes).")
            return
        if count:
            overdue.update(status=CoachInvite.Status.EXPIRED)
        self.stdout.write(self.style.SUCCESS(f"Expired {count} overdue invite(s)."))
