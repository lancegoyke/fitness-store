"""Reap expired demo-sandbox coach accounts past their TTL (issue #389, Phase 2).

A ``/meso/demo/`` sandbox (``SandboxSession``) lives ``MESO_SANDBOX_TTL_HOURS``;
after that its throwaway coach — and everything hanging off it, **including the
five demo-athlete users** (separate rows ``demo.clear_demo`` removes explicitly;
they don't cascade from the coach) — is deleted. Guards already treat the
sandbox as read-only-to-the-world (no email/push/Stripe), so reaping is purely a
DB cleanup.

Idempotent and best-effort per sandbox; safe to run on a cron.

    manage.py meso_expire_sandboxes
    manage.py meso_expire_sandboxes --dry-run    # report the count, change nothing
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from store_project.meso import sandbox
from store_project.meso.models import SandboxSession


class Command(BaseCommand):
    help = "Delete expired demo-sandbox coach accounts and their seeded data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many sandboxes would be reaped without changing anything.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            count = SandboxSession.objects.filter(
                expires_at__lte=timezone.now()
            ).count()
            self.stdout.write(f"{count} sandbox(es) expired (dry run — no changes).")
            return
        reaped = sandbox.expire_sandboxes()
        self.stdout.write(self.style.SUCCESS(f"Reaped {reaped} expired sandbox(es)."))
