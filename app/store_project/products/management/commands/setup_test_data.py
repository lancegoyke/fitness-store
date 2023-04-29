from django.core.management.base import BaseCommand
from django.db import transaction
from store_project.pages.factories import PageFactory
from store_project.pages.models import Page
from store_project.products.factories import ProgramFactory
from store_project.products.models import Program
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory
from store_project.users.models import User

NUM_PROGRAMS = 35
NUM_USERS = 10


class Command(BaseCommand):
    help = "Generates test products"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.stdout.write("Deleting old data...")
        models = [User, Program, Page]
        for m in models:
            m.objects.all().delete()

        self.stdout.write("Creating new data...")
        # Create the superuser
        superuser = SuperAdminFactory()
        self.stdout.write(f"  - new superuser {superuser.username}")

        # Create the other users
        people = []
        for _ in range(NUM_USERS):
            person = UserFactory()
            people.append(person)
        self.stdout.write(f"  - {NUM_USERS} new users")

        # Create the About page
        PageFactory()
        self.stdout.write("  - new About page")

        # Create the programs
        for _ in range(NUM_PROGRAMS):
            ProgramFactory()
        self.stdout.write(f"  - {NUM_PROGRAMS} new programs")
