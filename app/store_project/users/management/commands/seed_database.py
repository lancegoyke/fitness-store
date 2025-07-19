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

    @transaction.atomic
    def handle(self, *args, **options):
        delete_flag = options.get("delete", False)
        challenges_only = options.get("challenges_only", False)
        products_only = options.get("products_only", False)

        if challenges_only and products_only:
            self.stdout.write(
                self.style.ERROR(
                    "Cannot use both --challenges-only and --products-only flags"
                )
            )
            return

        self.stdout.write(self.style.SUCCESS("Starting database seeding process..."))

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
