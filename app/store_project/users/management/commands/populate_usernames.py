from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Populate empty username fields with email prefixes"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Find users with empty or null usernames
        users_with_empty_usernames = User.objects.filter(username__in=["", None])
        count = users_with_empty_usernames.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS("No users found with empty usernames.")
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"DRY RUN: Would update {count} users")
            )
            for user in users_with_empty_usernames:
                if user.email:
                    base_username = user.email.split("@")[0]
                    username = base_username
                    counter = 1

                    while User.objects.filter(username=username).exists():
                        username = f"{base_username}{counter}"
                        counter += 1

                    self.stdout.write(f"  {user.email} -> {username}")
                else:
                    self.stdout.write(f"  {user.id} (no email) -> skipped")
            return

        updated_count = 0
        skipped_count = 0

        for user in users_with_empty_usernames:
            if user.email:
                # Extract username from email (part before @)
                base_username = user.email.split("@")[0]

                # Ensure username is unique by appending numbers if needed
                username = base_username
                counter = 1

                while User.objects.filter(username=username).exists():
                    username = f"{base_username}{counter}"
                    counter += 1

                user.username = username
                user.save(update_fields=["username"])
                updated_count += 1

                self.stdout.write(f"Updated {user.email} -> {username}")
            else:
                skipped_count += 1
                self.stdout.write(f"Skipped user {user.id} (no email)")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully updated {updated_count} users. "
                f"Skipped {skipped_count} users without email addresses."
            )
        )
