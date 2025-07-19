from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction


class Command(BaseCommand):
    help = "Seeds the entire database with sample data from all apps"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete all existing data before seeding the database",
        )
        parser.add_argument(
            "--challenges-only",
            action="store_true",
            help="Only seed challenges data",
        )
        parser.add_argument(
            "--products-only",
            action="store_true",
            help="Only seed products data",
        )
        parser.add_argument(
            "--users-only",
            action="store_true",
            help="Only seed users data",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        delete_flag = options.get("delete", False)
        challenges_only = options.get("challenges_only", False)
        products_only = options.get("products_only", False)
        users_only = options.get("users_only", False)

        exclusive_flags = [challenges_only, products_only, users_only]
        if sum(exclusive_flags) > 1:
            self.stdout.write(
                self.style.ERROR(
                    "Cannot use multiple exclusive flags (--challenges-only, --products-only, --users-only)"
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("Starting database seeding process..."))

        # Seed users data first (required by other commands)
        if users_only or not (challenges_only or products_only):
            self.stdout.write("Seeding users data...")
            try:
                if delete_flag:
                    call_command("seed_users", "--delete")
                else:
                    call_command("seed_users")
                self.stdout.write(
                    self.style.SUCCESS("✓ Users data seeded successfully")
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"✗ Failed to seed users data: {e}"))
                return

        # Exit early if users-only flag is set
        if users_only:
            self.stdout.write(self.style.SUCCESS("🎉 User seeding completed!"))
            return

        # Seed challenges data
        if not products_only:
            self.stdout.write("Seeding challenges data...")
            try:
                if delete_flag:
                    call_command("seed_challenges", "--delete")
                else:
                    call_command("seed_challenges")
                self.stdout.write(
                    self.style.SUCCESS("✓ Challenges data seeded successfully")
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Failed to seed challenges data: {e}")
                )
                return

        # Seed products data
        if not challenges_only:
            self.stdout.write("Seeding products data...")
            try:
                if delete_flag:
                    call_command("seed_products", "--refresh")
                else:
                    call_command("seed_products")
                self.stdout.write(
                    self.style.SUCCESS("✓ Products data seeded successfully")
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"✗ Failed to seed products data: {e}")
                )
                return

        self.stdout.write(self.style.SUCCESS("🎉 All seeding completed successfully!"))
