import random
from datetime import datetime
from datetime import timedelta
from textwrap import dedent

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from store_project.challenges.models import Challenge
from store_project.challenges.models import DifficultyLevel
from store_project.challenges.models import Record
from store_project.users.models import User
from taggit.models import Tag
from taggit.models import TaggedItem

TAG_OPTIONS = [
    "Strength",
    "Endurance",
    "Cardio",
    "Flexibility",
    "Balance",
    "Agility",
    "Speed",
    "Power",
    "Coordination",
    "Accuracy",
]


class Command(BaseCommand):
    help = "Seeds the database with challenges and related data"

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete all existing data before seeding the database",
        )

    def _create_challenges(self) -> list[Challenge]:
        """Returns a Challenge object, but does not save it to the databse."""
        num_objects = 25
        challenges = []
        for n in range(num_objects):
            name = f"Challenge {n + 1}"
            description = dedent(
                f"""\
                Bodyweight squat x {random.randint(5, 20)}
                Push up x {random.randint(5, 20)}
                Walking lunge x {random.randint(5, 20)} each
                Burpees x {random.randint(5, 20)}
                Front plank x 30 sec
                """
            )
            slug = slugify(name)
            challenge = Challenge(
                name=name,
                description=description,
                summary="Lorem ipsum dolor sit amet.",
                slug=slug,
                difficulty_level=random.choice(list(DifficultyLevel.values)),
            )
            challenges.append(challenge)
        return Challenge.objects.bulk_create(challenges, batch_size=100)

    def _create_records(self, challenges: list[Challenge]) -> list[Record]:
        """Creates Record objects for each Challenge object."""
        # Pre-fetch all users once to avoid slow random queries
        all_users = list(User.objects.all())
        if not all_users:
            return []

        records = []
        for challenge in challenges:
            num_records = random.randint(5, 250)
            for _ in range(num_records):
                # Create a timedelta object for the DurationField
                minutes = random.randint(10, 35)
                seconds = random.randint(0, 59)
                time_score = timedelta(minutes=minutes, seconds=seconds)

                # Generate random date upfront instead of after creation
                naive_datetime = datetime.now() - timedelta(days=random.randint(0, 365))
                aware_datetime = timezone.make_aware(
                    naive_datetime, timezone=timezone.get_current_timezone()
                )

                records.append(
                    Record(
                        challenge=challenge,
                        time_score=time_score,
                        user=random.choice(
                            all_users
                        ),  # Much faster than DB random query
                        date_recorded=aware_datetime,
                    )
                )

        record_objs = Record.objects.bulk_create(records, batch_size=1000)
        return record_objs

    @transaction.atomic
    def _tag_challenges(self, challenges: list[Challenge]) -> None:
        # Batch tag additions in a single transaction for better performance
        for challenge in challenges:
            challenge.tags.add(*random.sample(TAG_OPTIONS, random.randint(1, 3)))

    def _cleanup_taggit_data(self):
        """Clean up taggit tables to prevent constraint violations."""
        # Delete ALL taggit records to ensure clean state
        TaggedItem.objects.all().delete()
        Tag.objects.all().delete()

    @transaction.atomic
    def handle(self, *args, **kwargs):
        if kwargs["delete"]:
            # Delete challenges and related records
            Challenge.objects.all().delete()

            # Clean up taggit tables and reset sequences
            self._cleanup_taggit_data()

            self.stdout.write(self.style.SUCCESS("Challenge data deleted"))

        if Challenge.objects.exists():
            self.stdout.write(
                self.style.WARNING(
                    "Challenge data already exists. Use --delete to delete all data first"
                )
            )
            return

        # Ensure users exist by calling seed_users
        if not User.objects.exists():
            self.stdout.write("No users found, creating users first...")
            call_command("seed_users")

        challenges = self._create_challenges()
        self._tag_challenges(challenges)
        self._create_records(challenges)

        self.stdout.write(self.style.SUCCESS("Done 💪"))
