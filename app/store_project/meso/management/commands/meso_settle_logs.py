"""Settle quiet PENDING session logs to DONE past the 24h window (5b, settle.py).

A ``SessionLog`` that's PENDING, holds at least one ``LoggedSet`` (typed or
structured), and hasn't been touched in ``MESO_SETTLE_QUIET_HOURS`` gets
promoted to DONE — the athlete's typed-then-abandoned session finally counts
for the DONE-only reads (adherence, persisted 1RM, coach results, the agent's
grounding). See ``settle.py``'s module docstring for the full contract.

Idempotent and best-effort per log; safe to run on a cron.

    manage.py meso_settle_logs
    manage.py meso_settle_logs --dry-run    # report the count, change nothing
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from store_project.meso import settle


class Command(BaseCommand):
    help = "Flip quiet PENDING session logs (with at least one logged set) to DONE."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many logs would settle without changing anything.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            cutoff = timezone.now() - settle.quiet_period()
            count = settle.settleable_logs(cutoff).count()
            self.stdout.write(f"{count} log(s) would settle (dry run — no changes).")
            return
        settled = settle.settle_quiet_logs()
        self.stdout.write(self.style.SUCCESS(f"Settled {settled} quiet log(s)."))
