import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone
from store_project.challenges.models import Challenge
from store_project.challenges.models import Record
from store_project.users.models import User


class Command(BaseCommand):
    help = "Fix historical timestamps from production data by bypassing auto_now_add constraints"

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            type=str,
            default="data-import",
            help="Directory containing JSON data files (default: data-import)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without actually updating",
        )
        parser.add_argument(
            "--challenges-only",
            action="store_true",
            help="Only fix challenge timestamps",
        )
        parser.add_argument(
            "--records-only",
            action="store_true",
            help="Only fix record timestamps",
        )
        parser.add_argument(
            "--users-only",
            action="store_true",
            help="Only fix user timestamps",
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        dry_run = options["dry_run"]
        challenges_only = options["challenges_only"]
        records_only = options["records_only"]
        users_only = options["users_only"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No data will be updated")
            )

        # Determine what to update
        update_challenges = not (records_only or users_only)
        update_records = not (challenges_only or users_only)
        update_users = not (challenges_only or records_only)

        if challenges_only:
            update_challenges = True
            update_records = False
            update_users = False
        elif records_only:
            update_challenges = False
            update_records = True
            update_users = False
        elif users_only:
            update_challenges = False
            update_records = False
            update_users = True

        # Track statistics
        stats = {
            "challenges_updated": 0,
            "records_updated": 0,
            "users_updated": 0,
            "challenges_missing": 0,
            "records_missing": 0,
            "users_missing": 0,
        }

        with transaction.atomic():
            if update_challenges:
                self.stdout.write("Fixing challenge timestamps...")
                stats.update(self.fix_challenge_timestamps(data_dir, dry_run))

            if update_records:
                self.stdout.write("Fixing record timestamps...")
                stats.update(self.fix_record_timestamps(data_dir, dry_run))

            if update_users:
                self.stdout.write("Fixing user timestamps...")
                stats.update(self.fix_user_timestamps(data_dir, dry_run))

            # Report results
            self.report_results(stats, dry_run)

            if dry_run:
                self.stdout.write(
                    self.style.WARNING("DRY RUN COMPLETE - No data was updated")
                )
                raise Exception("Dry run - rolling back transaction")
            else:
                self.stdout.write(
                    self.style.SUCCESS("Historical timestamps fixed successfully!")
                )

    def fix_challenge_timestamps(self, data_dir, dry_run):
        filepath = os.path.join(data_dir, "production-challenges.json")
        if not os.path.exists(filepath):
            self.stdout.write(
                self.style.WARNING(f"File not found: {filepath}. Skipping challenges.")
            )
            return {"challenges_updated": 0, "challenges_missing": 0}

        with open(filepath, "r") as f:
            data = json.load(f)

        updated_count = 0
        missing_count = 0

        for item in data:
            if item["model"] != "challenges.challenge":
                continue

            challenge_id = item["pk"]
            production_date = item["fields"]["date_created"]

            # Parse the production timestamp
            production_datetime = self.parse_datetime(production_date)

            if dry_run:
                # Check if challenge exists
                if Challenge.objects.filter(id=challenge_id).exists():
                    challenge = Challenge.objects.get(id=challenge_id)
                    current_date = challenge.date_created
                    self.stdout.write(
                        f"  Would update Challenge {challenge_id} ({challenge.name}): "
                        f"{current_date} -> {production_datetime}"
                    )
                    updated_count += 1
                else:
                    self.stdout.write(
                        f"  Challenge {challenge_id} not found in database"
                    )
                    missing_count += 1
            else:
                # Use update() to bypass auto_now_add
                updated = Challenge.objects.filter(id=challenge_id).update(
                    date_created=production_datetime
                )
                if updated:
                    updated_count += 1
                else:
                    missing_count += 1

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Updated {updated_count} challenges, {missing_count} not found"
                )
            )

        return {
            "challenges_updated": updated_count,
            "challenges_missing": missing_count,
        }

    def fix_record_timestamps(self, data_dir, dry_run):
        filepath = os.path.join(data_dir, "production-records.json")
        if not os.path.exists(filepath):
            self.stdout.write(
                self.style.WARNING(f"File not found: {filepath}. Skipping records.")
            )
            return {"records_updated": 0, "records_missing": 0}

        with open(filepath, "r") as f:
            data = json.load(f)

        updated_count = 0
        missing_count = 0

        for item in data:
            if item["model"] != "challenges.record":
                continue

            record_id = item["pk"]
            production_date = item["fields"]["date_recorded"]

            # Parse the production timestamp
            production_datetime = self.parse_datetime(production_date)

            if dry_run:
                # Check if record exists
                if Record.objects.filter(id=record_id).exists():
                    record = Record.objects.get(id=record_id)
                    current_date = record.date_recorded
                    challenge_name = (
                        record.challenge.name if record.challenge else "Unknown"
                    )
                    user_email = record.user.email if record.user else "Unknown"
                    self.stdout.write(
                        f"  Would update Record {record_id} ({challenge_name} by {user_email}): "
                        f"{current_date} -> {production_datetime}"
                    )
                    updated_count += 1
                else:
                    self.stdout.write(f"  Record {record_id} not found in database")
                    missing_count += 1
            else:
                # Use update() to bypass auto_now_add
                updated = Record.objects.filter(id=record_id).update(
                    date_recorded=production_datetime
                )
                if updated:
                    updated_count += 1
                else:
                    missing_count += 1

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Updated {updated_count} records, {missing_count} not found"
                )
            )

        return {"records_updated": updated_count, "records_missing": missing_count}

    def fix_user_timestamps(self, data_dir, dry_run):
        filepath = os.path.join(data_dir, "production-users.json")
        if not os.path.exists(filepath):
            self.stdout.write(
                self.style.WARNING(f"File not found: {filepath}. Skipping users.")
            )
            return {"users_updated": 0, "users_missing": 0}

        with open(filepath, "r") as f:
            data = json.load(f)

        updated_count = 0
        missing_count = 0

        for item in data:
            if item["model"] != "users.customuser":
                continue

            user_id = item["pk"]
            production_date = item["fields"]["date_joined"]

            # Parse the production timestamp
            production_datetime = self.parse_datetime(production_date)

            if dry_run:
                # Check if user exists
                if User.objects.filter(id=user_id).exists():
                    user = User.objects.get(id=user_id)
                    current_date = user.date_joined
                    self.stdout.write(
                        f"  Would update User {user.email or user.username}: "
                        f"{current_date} -> {production_datetime}"
                    )
                    updated_count += 1
                else:
                    self.stdout.write(f"  User {user_id} not found in database")
                    missing_count += 1
            else:
                # Use update() to bypass auto_now_add
                updated = User.objects.filter(id=user_id).update(
                    date_joined=production_datetime
                )
                if updated:
                    updated_count += 1
                else:
                    missing_count += 1

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Updated {updated_count} users, {missing_count} not found"
                )
            )

        return {"users_updated": updated_count, "users_missing": missing_count}

    def parse_datetime(self, datetime_str):
        """Parse datetime string from production data."""
        try:
            # Handle ISO format with timezone
            dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            # Ensure it's timezone aware
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt)
            return dt
        except ValueError as e:
            raise CommandError(f"Could not parse datetime '{datetime_str}': {e}")

    def report_results(self, stats, dry_run):
        """Report the results of the timestamp fixing operation."""
        action = "Would update" if dry_run else "Updated"

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.SUCCESS("📊 TIMESTAMP FIX SUMMARY"))
        self.stdout.write("=" * 60)

        if stats["challenges_updated"] or stats["challenges_missing"]:
            self.stdout.write(
                f"🎯 Challenges: {action} {stats['challenges_updated']}, "
                f"{stats['challenges_missing']} missing"
            )

        if stats["records_updated"] or stats["records_missing"]:
            self.stdout.write(
                f"📋 Records: {action} {stats['records_updated']}, "
                f"{stats['records_missing']} missing"
            )

        if stats["users_updated"] or stats["users_missing"]:
            self.stdout.write(
                f"👥 Users: {action} {stats['users_updated']}, "
                f"{stats['users_missing']} missing"
            )

        total_updated = (
            stats["challenges_updated"]
            + stats["records_updated"]
            + stats["users_updated"]
        )

        self.stdout.write(f"\n🔄 Total: {action} {total_updated} timestamps")
        self.stdout.write("=" * 60)
