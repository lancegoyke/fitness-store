import os

from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db.models import Q
from store_project.challenges.models import Challenge
from store_project.challenges.services import generate_challenge_summary


class Command(BaseCommand):
    help = "Generate AI summaries (via Google Gemini) for challenges missing one."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List challenges that would get a summary without calling the API.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Only process the first N challenges (handy for capping API cost).",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Regenerate summaries for all challenges, even ones already set.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        overwrite = options["overwrite"]

        if overwrite:
            challenges = Challenge.objects.all()
        else:
            challenges = Challenge.objects.filter(
                Q(summary="") | Q(summary__isnull=True)
            )
        challenges = challenges.order_by("name")

        if limit is not None:
            challenges = challenges[:limit]

        total = challenges.count()
        if total == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "All challenges already have a summary. Nothing to do."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN - {total} challenge(s) would get a summary:"
                )
            )
            for challenge in challenges:
                self.stdout.write(f"  - {challenge.name}")
            return

        if not os.environ.get("GOOGLE_API_KEY"):
            self.stderr.write(
                self.style.ERROR("GOOGLE_API_KEY not configured. Aborting.")
            )
            return

        self.stdout.write(f"Generating summaries for {total} challenge(s)...")

        succeeded = 0
        skipped = 0
        failed = 0
        for challenge in challenges:
            if not challenge.description.strip():
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠ Skipping '{challenge.name}': empty description"
                    )
                )
                skipped += 1
                continue

            try:
                summary = generate_challenge_summary(challenge.description)
            except Exception as exc:  # external API errors, keep going
                self.stderr.write(
                    self.style.ERROR(f"✗ Failed '{challenge.name}': {exc}")
                )
                failed += 1
                continue

            challenge.summary = summary
            challenge.save(update_fields=["summary"])
            self.stdout.write(self.style.SUCCESS(f"✓ {challenge.name}"))
            succeeded += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {succeeded} generated, {skipped} skipped, {failed} failed."
            )
        )
