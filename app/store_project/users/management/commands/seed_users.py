from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction

from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory
from store_project.users.models import User

RANDOM_USERS = [
    "John",
    "Emma",
    "Michael",
    "Olivia",
    "William",
    "Ava",
    "James",
    "Isabella",
    "Benjamin",
    "Sophia",
    "Mason",
    "Mia",
    "Elijah",
    "Charlotte",
    "Oliver",
    "Amelia",
    "Jacob",
    "Harper",
    "Lucas",
    "Evelyn",
    "Alexander",
    "Abigail",
    "Daniel",
    "Emily",
    "Matthew",
]


class Command(BaseCommand):
    help = "Seeds the database with users and authentication setup"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete all existing users before seeding",
        )
        parser.add_argument(
            "--superuser-only",
            action="store_true",
            help="Only create superuser, skip regular users",
        )

    def _create_social_app(self) -> None:
        """Creates a SocialApp object for Google OAuth in Django admin."""
        if SocialApp.objects.filter(provider="google").exists():
            return

        site = Site.objects.get_current()
        google = SocialApp.objects.create(
            provider="google",
            name="Google",
            client_id="1234567890",
            secret="0987654321",
        )
        google.sites.add(site)
        google.save()

    def _create_superuser(self) -> User:
        """Creates superuser using factory if none exists."""
        existing_superuser = User.objects.filter(is_superuser=True).first()
        if existing_superuser:
            self.stdout.write(
                f"  - using existing superuser: {existing_superuser.username}"
            )
            return existing_superuser

        superuser = SuperAdminFactory()
        self.stdout.write(f"  - created superuser: {superuser.username}")
        return superuser

    def _create_regular_users(self) -> list[User]:
        """Creates regular users from predefined list and factories."""
        existing_count = User.objects.filter(is_superuser=False).count()

        users = []

        # Create users from RANDOM_USERS list (for challenges compatibility)
        for username in RANDOM_USERS:
            email = f"{username.lower()}@example.com"
            if not User.objects.filter(email=email).exists():
                user = User(username=username.lower(), email=email)
                user.set_password("testpass123")
                users.append(user)

        if users:
            created_users = User.objects.bulk_create(users, batch_size=100)
            self.stdout.write(
                f"  - created {len(created_users)} users from RANDOM_USERS list"
            )

        # Create additional users via factory if needed (for products compatibility)
        factory_users_needed = max(0, 10 - existing_count - len(users))
        if factory_users_needed > 0:
            factory_users = UserFactory.create_batch(factory_users_needed)
            self.stdout.write(
                f"  - created {len(factory_users)} additional users via factory"
            )
            users.extend(factory_users)

        return users

    @transaction.atomic
    def handle(self, *args, **options):
        if options["delete"]:
            User.objects.all().delete()
            SocialApp.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("All users and social apps deleted"))

        if User.objects.exists() and not options["delete"]:
            self.stdout.write(
                self.style.WARNING(
                    "Users already exist. Use --delete to delete all users first"
                )
            )
            return

        self.stdout.write("Creating users...")

        # Always create superuser
        self._create_superuser()

        # Create regular users unless --superuser-only flag is used
        users = []
        if not options["superuser_only"]:
            users = self._create_regular_users()

        # Create social app for authentication
        self._create_social_app()
        self.stdout.write("  - created/verified Google social app")

        total_users = 1 + len(users)  # superuser + regular users
        self.stdout.write(
            self.style.SUCCESS(f"✓ User seeding completed! Total users: {total_users}")
        )
