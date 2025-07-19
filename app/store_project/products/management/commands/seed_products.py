from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction
from store_project.exercises.factories import ExerciseFactory
from store_project.exercises.models import Exercise
from store_project.pages.factories import PageFactory
from store_project.pages.models import Page
from store_project.products.factories import BookFactory
from store_project.products.factories import ProgramFactory
from store_project.products.models import Book
from store_project.products.models import Program
from store_project.users.factories import SuperAdminFactory
from store_project.users.factories import UserFactory
from store_project.users.models import User

NUM_PROGRAMS = 35
NUM_USERS = 10
NUM_EXERCISES = 300
NUM_BOOKS = 15


class Command(BaseCommand):
    help = "Seeds database with products and related data"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Deletes all data from the database before seeding",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if Program.objects.exists() and not options["refresh"]:
            self.stdout.write(
                self.style.WARNING(
                    "Data already exists. Use --refresh to delete all data first"
                )
            )
            return

        if options["refresh"]:
            self.stdout.write("Deleting old data...")
            models = [User, Program, Page, Exercise, Book]
            for m in models:
                m.objects.all().delete()

        self.stdout.write("Creating new data...")

        superuser, users = create_users()
        self.stdout.write(f"  - new superuser {superuser.username}")
        self.stdout.write(f"  - {len(users)} new users")

        PageFactory()
        self.stdout.write("  - new About page")

        programs = create_programs()
        self.stdout.write(f"  - {len(programs)} new programs")

        books = create_books()
        self.stdout.write(f"  - {len(books)} new books")

        exercises = create_exercises()
        self.stdout.write(f"  - {len(exercises)} new exercises")

        self.stdout.write("Done 💪")


@transaction.atomic
def create_users() -> tuple[SuperAdminFactory, list[UserFactory]]:
    """Creates a superuser and NUM_USERS users."""
    superuser = SuperAdminFactory()
    people = UserFactory.create_batch(NUM_USERS)

    return superuser, people


@transaction.atomic
def create_programs() -> list[ProgramFactory]:
    """Creates NUM_PROGRAMS programs."""
    return ProgramFactory.create_batch(NUM_PROGRAMS)


@transaction.atomic
def create_books() -> list[BookFactory]:
    """Creates NUM_BOOKS books."""
    return BookFactory.create_batch(NUM_BOOKS)


@transaction.atomic
def create_exercises() -> list[ExerciseFactory]:
    """Creates NUM_EXERCISES exercises."""
    return ExerciseFactory.create_batch(NUM_EXERCISES)
