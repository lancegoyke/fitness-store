from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from store_project.challenges.models import Record
from store_project.pages.models import Page
from store_project.products.models import Book
from store_project.products.models import Program

User = get_user_model()


class Command(BaseCommand):
    help = "Merge one user account into another, transferring all related data"

    def add_arguments(self, parser):
        parser.add_argument(
            "source_email",
            type=str,
            help="Email of the user account to merge FROM (will be deleted)",
        )
        parser.add_argument(
            "target_email",
            type=str,
            help="Email of the user account to merge TO (will be kept)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be transferred without making changes",
        )

    def handle(self, *args, **options):
        source_email = options["source_email"]
        target_email = options["target_email"]
        dry_run = options["dry_run"]

        if source_email == target_email:
            raise CommandError("Source and target emails cannot be the same")

        try:
            source_user = User.objects.get(email=source_email)
        except User.DoesNotExist:
            raise CommandError(f"Source user with email '{source_email}' not found")

        try:
            target_user = User.objects.get(email=target_email)
        except User.DoesNotExist:
            raise CommandError(f"Target user with email '{target_email}' not found")

        # Count related objects
        pages_count = Page.objects.filter(author=source_user).count()
        records_count = Record.objects.filter(user=source_user).count()
        programs_count = Program.objects.filter(author=source_user).count()
        books_count = Book.objects.filter(author=source_user).count()
        products_count = programs_count + books_count

        self.stdout.write("\nUser Merge Plan:")
        self.stdout.write(f"Source: {source_user.email} (ID: {source_user.id})")
        self.stdout.write(f"Target: {target_user.email} (ID: {target_user.id})")
        self.stdout.write("\nData to transfer:")
        self.stdout.write(f"- Pages: {pages_count}")
        self.stdout.write(f"- Challenge Records: {records_count}")
        self.stdout.write(f"- Programs: {programs_count}")
        self.stdout.write(f"- Books: {books_count}")
        self.stdout.write(f"- Total Products: {products_count}")

        # Show user data comparison
        self.stdout.write("\nUser Data Comparison:")
        self.stdout.write(f"Name: '{source_user.name}' → '{target_user.name}'")
        self.stdout.write(f"Points: {source_user.points} → {target_user.points}")
        self.stdout.write(f"Birthday: {source_user.birthday} → {target_user.birthday}")
        self.stdout.write(f"Sex: {source_user.sex} → {target_user.sex}")
        self.stdout.write(
            f"Stripe Customer ID: '{source_user.stripe_customer_id}' → '{target_user.stripe_customer_id}'"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes will be made"))
            return

        # Confirm before proceeding
        confirm = input(
            f"\nAre you sure you want to merge {source_email} into {target_email}? (yes/no): "
        )
        if confirm.lower() != "yes":
            self.stdout.write("Merge cancelled")
            return

        try:
            with transaction.atomic():
                # Transfer pages
                if pages_count > 0:
                    Page.objects.filter(author=source_user).update(author=target_user)
                    self.stdout.write(f"✓ Transferred {pages_count} pages")

                # Transfer challenge records
                if records_count > 0:
                    Record.objects.filter(user=source_user).update(user=target_user)
                    self.stdout.write(
                        f"✓ Transferred {records_count} challenge records"
                    )

                # Transfer programs and books
                if programs_count > 0:
                    Program.objects.filter(author=source_user).update(
                        author=target_user
                    )
                    self.stdout.write(f"✓ Transferred {programs_count} programs")

                if books_count > 0:
                    Book.objects.filter(author=source_user).update(author=target_user)
                    self.stdout.write(f"✓ Transferred {books_count} books")

                # Merge user data (keep target's data, but add source's points)
                target_user.points += source_user.points

                # Only update target user fields if they're empty and source has data
                if not target_user.name and source_user.name:
                    target_user.name = source_user.name
                if not target_user.birthday and source_user.birthday:
                    target_user.birthday = source_user.birthday
                if (
                    target_user.sex == User.Sex.UNKNOWN
                    and source_user.sex != User.Sex.UNKNOWN
                ):
                    target_user.sex = source_user.sex
                if (
                    not target_user.stripe_customer_id
                    and source_user.stripe_customer_id
                ):
                    target_user.stripe_customer_id = source_user.stripe_customer_id

                target_user.save()
                self.stdout.write("✓ Updated target user data")

                # Delete source user
                source_user.delete()
                self.stdout.write(f"✓ Deleted source user {source_email}")

                self.stdout.write(
                    self.style.SUCCESS(
                        f"\nSuccessfully merged {source_email} into {target_email}"
                    )
                )

        except Exception as e:
            raise CommandError(f"Error during merge: {str(e)}")
