import json
import os

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from store_project.challenges.models import Challenge
from store_project.challenges.models import ChallengeTag


class Command(BaseCommand):
    help = "Import challenge tags from production data exports"

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
            help="Show what would be imported without actually importing",
        )
        parser.add_argument(
            "--clear-existing-tags",
            action="store_true",
            help="Remove all existing ChallengeTag objects before importing",
        )
        parser.add_argument(
            "--preserve-existing",
            action="store_true",
            help="Keep existing tags and merge with production data (default behavior)",
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        dry_run = options["dry_run"]
        clear_existing = options["clear_existing_tags"]
        preserve_existing = options["preserve_existing"]

        if clear_existing and preserve_existing:
            raise CommandError(
                "Cannot use both --clear-existing-tags and --preserve-existing"
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No data will be imported")
            )

        if clear_existing:
            self.stdout.write(
                self.style.WARNING(
                    "CLEAR MODE - Existing ChallengeTag objects will be removed"
                )
            )
        elif preserve_existing:
            self.stdout.write(
                self.style.SUCCESS(
                    "PRESERVE MODE - Existing tags will be kept and merged"
                )
            )

        tags_file = os.path.join(data_dir, "production-tags.json")
        tagged_items_file = os.path.join(data_dir, "production-tagged_items.json")

        if not os.path.exists(tags_file):
            raise CommandError(f"Tags file not found: {tags_file}")

        if not os.path.exists(tagged_items_file):
            raise CommandError(f"Tagged items file not found: {tagged_items_file}")

        with transaction.atomic():
            # Clear existing tags if requested
            if clear_existing:
                cleared_count, clear_messages = self.clear_existing_tags(dry_run)
                self.stdout.write(
                    self.style.WARNING(f"✓ Cleared {cleared_count} existing tags")
                )
                for message in clear_messages[:3]:
                    self.stdout.write(f"  {message}")

            # Show current state before import
            self.show_current_state()

            # Import tags first
            tag_count, tag_messages = self.import_challenge_tags(tags_file, dry_run)
            self.stdout.write(self.style.SUCCESS(f"✓ Processed {tag_count} tags"))
            for message in tag_messages[:5]:  # Show first 5 messages
                self.stdout.write(f"  {message}")
            if len(tag_messages) > 5:
                self.stdout.write(f"  ... and {len(tag_messages) - 5} more")

            # Import tag relationships
            relation_count, relation_messages = self.import_tag_relationships(
                tagged_items_file, data_dir, dry_run
            )
            self.stdout.write(
                self.style.SUCCESS(f"✓ Processed {relation_count} tag relationships")
            )
            for message in relation_messages[:5]:  # Show first 5 messages
                self.stdout.write(f"  {message}")
            if len(relation_messages) > 5:
                self.stdout.write(f"  ... and {len(relation_messages) - 5} more")

            if dry_run:
                self.stdout.write(
                    self.style.WARNING("DRY RUN COMPLETE - No data was imported")
                )
                # Show final dry run summary
                self.show_dry_run_summary(tag_count, relation_count)
                raise Exception("Dry run - rolling back transaction")
            else:
                self.stdout.write(
                    self.style.SUCCESS("Challenge tags imported successfully!")
                )
                # Show final state
                self.show_current_state()

    def import_challenge_tags(self, filepath, dry_run):
        """Import ChallengeTag instances from production tags."""
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []

        for item in data:
            if item["model"] != "taggit.tag":
                continue

            fields = item["fields"]
            tag_name = fields["name"]
            tag_slug = fields["slug"]

            if dry_run:
                messages.append(f"Would import tag: {tag_name}")
                count += 1
                continue

            # Create or get ChallengeTag
            challenge_tag, created = ChallengeTag.objects.get_or_create(
                name=tag_name, defaults={"slug": tag_slug}
            )

            if created:
                messages.append(f"Created tag: {tag_name}")
            else:
                messages.append(f"Tag already exists: {tag_name}")

            count += 1

        return count, messages

    def import_tag_relationships(self, filepath, data_dir, dry_run):
        """Import challenge-tag relationships from production tagged items."""
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []

        # Build tag mapping from production IDs to names first
        tag_mapping = self._build_tag_mapping(data_dir)

        for item in data:
            if item["model"] != "taggit.taggeditem":
                continue

            fields = item["fields"]

            # We know from the user that content_type 15 was for challenges in production
            # Skip if definitely not a challenge (we'll check if the challenge exists later)
            if fields["content_type"] != 15:
                continue

            tag_id = fields["tag"]
            challenge_id = fields["object_id"]

            if dry_run:
                tag_name = tag_mapping.get(tag_id, f"Unknown tag {tag_id}")
                messages.append(
                    f"Would link challenge {challenge_id} to tag '{tag_name}'"
                )
                count += 1
                continue

            try:
                # Find the challenge
                challenge = Challenge.objects.get(id=challenge_id)

                # Find the tag by the original tag name
                challenge_tag = self._find_challenge_tag_by_original_id(
                    tag_id, tag_mapping
                )

                if challenge_tag:
                    challenge.challenge_tags.add(challenge_tag)
                    messages.append(
                        f"Linked '{challenge.name}' to tag '{challenge_tag.name}'"
                    )
                else:
                    messages.append(f"Tag with original ID {tag_id} not found")

            except Challenge.DoesNotExist:
                messages.append(f"Challenge {challenge_id} not found")
                continue

            count += 1

        return count, messages

    def _build_tag_mapping(self, data_dir):
        """Build a mapping from original tag IDs to tag names."""
        mapping = {}
        tags_file = os.path.join(data_dir, "production-tags.json")

        try:
            with open(tags_file, "r") as f:
                data = json.load(f)

            for item in data:
                if item["model"] == "taggit.tag":
                    mapping[item["pk"]] = item["fields"]["name"]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        return mapping

    def _find_challenge_tag_by_original_id(self, original_tag_id, tag_mapping):
        """Find ChallengeTag by looking up the original tag ID."""
        tag_name = tag_mapping.get(original_tag_id)
        if tag_name:
            return ChallengeTag.objects.filter(name=tag_name).first()
        return None

    def clear_existing_tags(self, dry_run):
        """Clear all existing ChallengeTag objects and their relationships."""
        count = 0
        messages = []

        existing_tags = ChallengeTag.objects.all()
        existing_count = existing_tags.count()

        if dry_run:
            messages.append(
                f"Would delete {existing_count} existing ChallengeTag objects"
            )
            for tag in existing_tags[:5]:
                messages.append(f"Would delete: {tag.name}")
            if existing_count > 5:
                messages.append(f"... and {existing_count - 5} more")
            return existing_count, messages

        # Clear all challenge-tag relationships first (handled automatically by Django)
        # Then delete the tags
        for tag in existing_tags:
            messages.append(f"Deleted: {tag.name}")
            count += 1

        existing_tags.delete()
        return count, messages

    def show_current_state(self):
        """Show current database state for context."""
        challenge_count = Challenge.objects.count()
        tag_count = ChallengeTag.objects.count()

        # Count challenges with tags
        tagged_challenge_count = (
            Challenge.objects.filter(challenge_tags__isnull=False).distinct().count()
        )

        self.stdout.write("\n" + "=" * 50)
        self.stdout.write("CURRENT DATABASE STATE:")
        self.stdout.write(f"  Challenges: {challenge_count}")
        self.stdout.write(f"  ChallengeTag objects: {tag_count}")
        self.stdout.write(f"  Challenges with tags: {tagged_challenge_count}")

        if tag_count > 0 and tag_count <= 10:
            self.stdout.write("  Existing tags:")
            for tag in ChallengeTag.objects.all()[:10]:
                self.stdout.write(f"    - {tag.name}")

        self.stdout.write("=" * 50 + "\n")

    def show_dry_run_summary(self, tag_count, relation_count):
        """Show summary of what would be imported in dry run."""
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write("DRY RUN SUMMARY:")
        self.stdout.write(f"  Tags that would be imported: {tag_count}")
        self.stdout.write(
            f"  Challenge-tag relationships that would be created: {relation_count}"
        )

        # Show sample of production tags that would be imported
        try:
            with open("data-import/production-tags.json", "r") as f:
                import json

                data = json.load(f)
                production_tags = [
                    item["fields"]["name"]
                    for item in data
                    if item["model"] == "taggit.tag"
                ][:10]
                self.stdout.write("  Sample production tags:")
                for tag in production_tags:
                    self.stdout.write(f"    - {tag}")
                if len(production_tags) == 10:
                    self.stdout.write("    - ...")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        self.stdout.write("=" * 50 + "\n")
