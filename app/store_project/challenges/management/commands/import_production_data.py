import json
import os

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from store_project.challenges.models import Challenge
from store_project.challenges.models import Record
from store_project.users.models import User
from taggit.models import Tag
from taggit.models import TaggedItem


class Command(BaseCommand):
    help = "Safely import production data with intelligent user merging"

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
            "--merge-users",
            action="store_true",
            help="Merge users by email when possible, preserve all data",
        )

    def handle(self, *args, **options):
        data_dir = options["data_dir"]
        dry_run = options["dry_run"]
        merge_users = options["merge_users"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No data will be imported")
            )

        if merge_users:
            self.stdout.write(
                self.style.SUCCESS(
                    "USER MERGE MODE - Will intelligently merge users by email"
                )
            )

        # Import order (dependencies first)
        import_files = [
            ("production-users.json", self.import_users_safe),
            ("production-tags.json", self.import_tags),
            ("production-challenges.json", self.import_challenges),
            ("production-records.json", self.import_records_safe),
            ("production-tagged_items.json", self.import_tagged_items),
        ]

        with transaction.atomic():
            for filename, import_func in import_files:
                filepath = os.path.join(data_dir, filename)
                if not os.path.exists(filepath):
                    self.stdout.write(
                        self.style.WARNING(f"File not found: {filepath}. Skipping.")
                    )
                    continue

                self.stdout.write(f"Importing {filename}...")
                try:
                    count, messages = import_func(filepath, dry_run, merge_users)
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"✓ Processed {count} records from {filename}"
                        )
                    )
                    for message in messages:
                        self.stdout.write(f"  {message}")
                except Exception as e:
                    raise CommandError(f"Error importing {filename}: {str(e)}")

            if dry_run:
                self.stdout.write(
                    self.style.WARNING("DRY RUN COMPLETE - No data was imported")
                )

                # Report what would happen to passwords in dry run
                users_needing_reset = self._get_password_reset_list()
                if users_needing_reset:
                    self.stdout.write("\n" + "=" * 60)
                    self.stdout.write(
                        self.style.WARNING(
                            f"🔑 DRY RUN: {len(users_needing_reset)} users would need password resets"
                        )
                    )
                    self.stdout.write("=" * 60)
                    self.stdout.write(
                        "These users would get production passwords that won't work:"
                    )
                    for user in users_needing_reset[:10]:  # Show first 10
                        email = user["email"] or "No email"
                        self.stdout.write(
                            f"  📧 {email} (username: {user['username']})"
                        )
                    if len(users_needing_reset) > 10:
                        self.stdout.write(
                            f"  ... and {len(users_needing_reset) - 10} more"
                        )
                    self.stdout.write("=" * 60)

                raise Exception("Dry run - rolling back transaction")
            else:
                self.stdout.write(self.style.SUCCESS("All data imported successfully!"))

                # Report users needing password reset
                users_needing_reset = self._get_password_reset_list()
                if users_needing_reset:
                    self.stdout.write("\n" + "=" * 60)
                    self.stdout.write(
                        self.style.WARNING(
                            f"🔑 IMPORTANT: {len(users_needing_reset)} users need password resets"
                        )
                    )
                    self.stdout.write("=" * 60)
                    self.stdout.write(
                        "These users have production passwords that won't work with your new SECRET_KEY:"
                    )
                    self.stdout.write("")

                    for user in users_needing_reset:
                        email = user["email"] or "No email"
                        self.stdout.write(
                            f"  📧 {email} (username: {user['username']})"
                        )

                    self.stdout.write("")
                    self.stdout.write("💡 You should:")
                    self.stdout.write("   1. Send password reset emails to these users")
                    self.stdout.write("   2. Or provide them with temporary passwords")
                    self.stdout.write("   3. Or use Django admin to set new passwords")
                    self.stdout.write("=" * 60)

    def import_users_safe(self, filepath, dry_run, merge_users):
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []
        user_id_mapping = {}  # Track old UUID -> new UUID mappings
        users_needing_password_reset = []  # Track users with production passwords

        for item in data:
            if item["model"] != "users.customuser":
                continue

            fields = item["fields"]
            production_user_id = item["pk"]
            email = fields.get("email", "").strip()
            username = fields.get("username", "").strip()

            if dry_run:
                # Determine what would happen to this user
                existing_user_by_email = None
                if email:
                    existing_user_by_email = User.objects.filter(email=email).first()
                existing_user_by_id = User.objects.filter(id=production_user_id).first()

                if not existing_user_by_email and not existing_user_by_id:
                    # Would be a new user needing password reset
                    users_needing_password_reset.append(
                        {
                            "email": email,
                            "username": username,
                            "user_id": production_user_id,
                        }
                    )
                    messages.append(
                        f"Would create new user: {email or 'No email'} ({username}) - WOULD NEED PASSWORD RESET"
                    )
                else:
                    messages.append(
                        f"Would process existing user: {email or 'No email'} ({username})"
                    )
                count += 1
                continue

            # Strategy 1: Find existing user by email (primary identifier)
            existing_user_by_email = None
            if email:
                existing_user_by_email = User.objects.filter(email=email).first()

            # Strategy 2: Find existing user by UUID (exact match)
            existing_user_by_id = User.objects.filter(id=production_user_id).first()

            if existing_user_by_email and existing_user_by_id:
                if existing_user_by_email.id == existing_user_by_id.id:
                    # Same user - update with production data if needed
                    if merge_users:
                        self._update_user(existing_user_by_email, fields)
                        messages.append(f"Updated existing user: {email}")
                        user_id_mapping[production_user_id] = str(
                            existing_user_by_email.id
                        )
                        count += 1
                    else:
                        messages.append(f"Skipped existing user: {email}")
                else:
                    # Different users with same email - handle conflict
                    if merge_users:
                        # Merge into the email-based user, track the ID mapping
                        self._update_user(existing_user_by_email, fields)
                        messages.append(
                            f"Merged users: {email} (UUID mapping: {production_user_id} -> {existing_user_by_email.id})"
                        )
                        user_id_mapping[production_user_id] = str(
                            existing_user_by_email.id
                        )
                        count += 1
                    else:
                        messages.append(
                            f"Conflict: email {email} exists with different UUID"
                        )

            elif existing_user_by_email:
                # User exists by email but different UUID
                if merge_users:
                    self._update_user(existing_user_by_email, fields)
                    messages.append(
                        f"Merged by email: {email} (UUID mapping: {production_user_id} -> {existing_user_by_email.id})"
                    )
                    user_id_mapping[production_user_id] = str(existing_user_by_email.id)
                    count += 1
                else:
                    messages.append(f"Skipped: email {email} already exists")

            elif existing_user_by_id:
                # User exists by UUID but different/no email
                if merge_users:
                    self._update_user(existing_user_by_id, fields)
                    messages.append(f"Updated by UUID: {production_user_id}")
                    user_id_mapping[production_user_id] = str(existing_user_by_id.id)
                    count += 1
                else:
                    messages.append(
                        f"Skipped: UUID {production_user_id} already exists"
                    )

            else:
                # New user - safe to create
                try:
                    # Handle username conflicts
                    final_username = username
                    if User.objects.filter(username=username).exists():
                        counter = 1
                        while User.objects.filter(
                            username=f"{username}_{counter}"
                        ).exists():
                            counter += 1
                        final_username = f"{username}_{counter}"
                        messages.append(
                            f"Username conflict resolved: {username} -> {final_username}"
                        )

                    user = User(
                        id=production_user_id,
                        password=fields["password"],
                        last_login=fields.get("last_login"),
                        is_superuser=fields["is_superuser"],
                        username=final_username,
                        first_name=fields.get("first_name", ""),
                        last_name=fields.get("last_name", ""),
                        email=email,
                        is_staff=fields["is_staff"],
                        is_active=fields["is_active"],
                        date_joined=fields["date_joined"],
                        sex=fields.get("sex", "U"),
                        birthday=fields.get("birthday"),
                        points=fields.get("points", 0),
                    )
                    user.save()
                    user_id_mapping[production_user_id] = str(user.id)
                    users_needing_password_reset.append(
                        {
                            "email": email,
                            "username": final_username,
                            "user_id": str(user.id),
                        }
                    )
                    messages.append(
                        f"Created new user: {email or 'No email'} ({final_username}) - NEEDS PASSWORD RESET"
                    )
                    count += 1

                except Exception as e:
                    messages.append(f"Failed to create user {email}: {str(e)}")

        # Store mapping and password reset list for later use
        if not dry_run:
            self._store_user_mapping(user_id_mapping)

        # Always store password reset list (for dry run reporting too)
        self._store_password_reset_list(users_needing_password_reset)

        return count, messages

    def _update_user(self, user, fields):
        """Update existing user with production data (preserves existing password)."""
        # Update fields that should be preserved from production
        # NOTE: Password is NOT updated to preserve existing authentication
        user.last_login = fields.get("last_login") or user.last_login
        user.first_name = fields.get("first_name", "") or user.first_name
        user.last_name = fields.get("last_name", "") or user.last_name
        user.sex = fields.get("sex", user.sex)
        user.birthday = fields.get("birthday") or user.birthday
        user.points = max(fields.get("points", 0), user.points)  # Keep higher points
        user.save()

    def _store_user_mapping(self, mapping):
        """Store user ID mapping for records import."""
        self._user_id_mapping = mapping

    def _get_user_mapping(self):
        """Get stored user ID mapping."""
        return getattr(self, "_user_id_mapping", {})

    def _store_password_reset_list(self, users_list):
        """Store list of users needing password reset."""
        self._users_needing_password_reset = users_list

    def _get_password_reset_list(self):
        """Get list of users needing password reset."""
        return getattr(self, "_users_needing_password_reset", [])

    def import_tags(self, filepath, dry_run, merge_users):
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []

        for item in data:
            if item["model"] != "taggit.tag":
                continue

            fields = item["fields"]
            tag_id = item["pk"]

            if dry_run:
                messages.append(f"Would import tag: {fields['name']}")
                count += 1
                continue

            if Tag.objects.filter(id=tag_id).exists():
                messages.append(f"Tag {tag_id} already exists, skipping")
                continue

            # Check for name conflicts
            if Tag.objects.filter(name=fields["name"]).exists():
                messages.append(f'Tag name "{fields["name"]}" already exists, skipping')
                continue

            tag = Tag(id=tag_id, name=fields["name"], slug=fields["slug"])
            tag.save()
            count += 1

        return count, messages

    def import_challenges(self, filepath, dry_run, merge_users):
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []

        for item in data:
            if item["model"] != "challenges.challenge":
                continue

            fields = item["fields"]
            challenge_id = item["pk"]

            if dry_run:
                messages.append(f"Would import challenge: {fields['name']}")
                count += 1
                continue

            if Challenge.objects.filter(id=challenge_id).exists():
                messages.append(f"Challenge {challenge_id} already exists, skipping")
                continue

            # Handle slug conflicts
            slug = fields["slug"]
            if Challenge.objects.filter(slug=slug).exists():
                counter = 1
                while Challenge.objects.filter(slug=f"{slug}-{counter}").exists():
                    counter += 1
                slug = f"{slug}-{counter}"
                messages.append(f"Slug conflict resolved: {fields['slug']} -> {slug}")

            challenge = Challenge(
                id=challenge_id,
                name=fields["name"],
                description=fields["description"],
                slug=slug,
                date_created=fields["date_created"],
            )
            challenge.save()
            count += 1

        return count, messages

    def import_records_safe(self, filepath, dry_run, merge_users):
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []
        user_mapping = self._get_user_mapping()

        for item in data:
            if item["model"] != "challenges.record":
                continue

            fields = item["fields"]
            record_id = item["pk"]

            if dry_run:
                messages.append(f"Would import record: {record_id}")
                count += 1
                continue

            if Record.objects.filter(id=record_id).exists():
                messages.append(f"Record {record_id} already exists, skipping")
                continue

            # Get challenge
            try:
                challenge = Challenge.objects.get(id=fields["challenge"])
            except Challenge.DoesNotExist:
                messages.append(
                    f"Skipping record {record_id}: Challenge {fields['challenge']} not found"
                )
                continue

            # Get user with mapping
            user = None
            original_user_id = fields.get("user")
            if original_user_id:
                # First try the mapped user ID
                mapped_user_id = user_mapping.get(original_user_id, original_user_id)
                try:
                    user = User.objects.get(id=mapped_user_id)
                except User.DoesNotExist:
                    # Try original ID as fallback
                    try:
                        user = User.objects.get(id=original_user_id)
                    except User.DoesNotExist:
                        messages.append(
                            f"Skipping record {record_id}: User {original_user_id} not found"
                        )
                        continue

            record = Record(
                id=record_id,
                challenge=challenge,
                time_score=fields["time_score"],
                notes=fields.get("notes", ""),
                date_recorded=fields["date_recorded"],
                user=user,
            )
            record.save()
            count += 1

        return count, messages

    def import_tagged_items(self, filepath, dry_run, merge_users):
        with open(filepath, "r") as f:
            data = json.load(f)

        count = 0
        messages = []

        for item in data:
            if item["model"] != "taggit.taggeditem":
                continue

            fields = item["fields"]
            tagged_item_id = item["pk"]

            if dry_run:
                messages.append(f"Would import tagged item: {tagged_item_id}")
                count += 1
                continue

            if TaggedItem.objects.filter(id=tagged_item_id).exists():
                messages.append(
                    f"Tagged item {tagged_item_id} already exists, skipping"
                )
                continue

            try:
                tag = Tag.objects.get(id=fields["tag"])
                content_type = ContentType.objects.get(id=fields["content_type"])
            except (Tag.DoesNotExist, ContentType.DoesNotExist) as e:
                messages.append(f"Skipping tagged item {tagged_item_id}: {str(e)}")
                continue

            tagged_item = TaggedItem(
                id=tagged_item_id,
                tag=tag,
                content_type=content_type,
                object_id=fields["object_id"],
            )
            tagged_item.save()
            count += 1

        return count, messages
