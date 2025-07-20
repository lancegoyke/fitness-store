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

    def _update_site_for_development(self) -> None:
        """Updates the Site to use localhost for development."""
        try:
            site = Site.objects.get(pk=1)
            if site.domain == "example.com":
                site.domain = "localhost:8000"
                site.name = "localhost:8000"
                site.save()
                self.stdout.write("  - updated site domain to localhost:8000")
            else:
                self.stdout.write(f"  - site already configured: {site.domain}")
        except Site.DoesNotExist:
            # Create site if it doesn't exist
            Site.objects.create(pk=1, domain="localhost:8000", name="localhost:8000")
            self.stdout.write("  - created site: localhost:8000")

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
        """Creates regular users using factory."""
        users = []
        for name in RANDOM_USERS:
            user = UserFactory(username=name, email=f"{name.lower()}@example.com")
            users.append(user)

        self.stdout.write(f"  - created {len(users)} regular users")
        return users

    @transaction.atomic
    def handle(self, *args, **options):
        if options["delete"]:
            User.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("All users deleted"))

        if User.objects.exists() and not options["delete"]:
            self.stdout.write(
                self.style.WARNING(
                    "Users already exist. Use --delete to delete all users first"
                )
            )
            return

        self.stdout.write("Creating users...")

        # Always update site for development
        self._update_site_for_development()

        # Always create superuser
        self._create_superuser()

        # Create regular users unless --superuser-only flag is used
        users = []
        if not options["superuser_only"]:
            users = self._create_regular_users()

        # Note: SocialApp creation removed - using SOCIALACCOUNT_PROVIDERS from settings instead
        self.stdout.write("  - social auth configured via settings (not database)")

        total_users = 1 + len(users)  # superuser + regular users
        self.stdout.write(
            self.style.SUCCESS(f"✓ User seeding completed! Total users: {total_users}")
        )
