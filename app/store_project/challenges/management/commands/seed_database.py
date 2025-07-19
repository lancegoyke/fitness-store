import random
from datetime import datetime
from datetime import timedelta
from textwrap import dedent

from allauth.socialaccount.models import SocialApp
from challenges.models import Challenge
from challenges.models import Record
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.core.management.base import CommandParser
from django.utils import timezone
from users.models import CustomUser as User

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
    help = "Seeds the database with an initial superuser and Challenge object"

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
            challenge = Challenge(name=name, description=description)
            challenges.append(challenge)
        return Challenge.objects.bulk_create(challenges)

    def _create_records(self, challenges: list[Challenge]) -> list[Record]:
        """Creates Record objects for each Challenge object."""
        records = []
        for challenge in challenges:
            num_records = random.randint(5, 250)
            records.extend(
                Record(
                    challenge=challenge,
                    time_score=f"00:{random.randint(10, 35)}:{random.randint(0, 59)}",
                    user=User.objects.order_by("?").first(),
                )
                for _ in range(num_records)
            )
        record_objs = Record.objects.bulk_create(records)

        for record in record_objs:
            naive_datetime = datetime.now() - timedelta(days=random.randint(0, 365))
            aware_datetime = timezone.make_aware(
                naive_datetime, timezone=timezone.get_current_timezone()
            )
            record.date_recorded = aware_datetime
        Record.objects.bulk_update(record_objs, ["date_recorded"])

        return record_objs

    def _create_social_app(self) -> None:
        """Creates a SocialApp object for the Django admin."""
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
        return User.objects.create_superuser(
            "admin", "admin@example.com", "adminpassword"
        )

    def _create_users(self) -> list[User]:
        return User.objects.bulk_create(
            [
                User(username=username.lower(), password="testpass123")
                for username in RANDOM_USERS
            ]
        )

    def _tag_challenges(self, challenges: list[Challenge]) -> None:
        for challenge in challenges:
            challenge.tags.add(*random.sample(TAG_OPTIONS, random.randint(1, 3)))

    def handle(self, *args, **kwargs):
        if kwargs["delete"]:
            User.objects.all().delete()
            Challenge.objects.all().delete()
            SocialApp.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("All data deleted"))

        if (
            User.objects.exists()
            or Challenge.objects.exists()
            or SocialApp.objects.exists()
        ):
            self.stdout.write(
                self.style.WARNING(
                    "Data already exists. Use --delete to delete all data first"
                )
            )
            return

        self._create_superuser()
        self._create_users()
        challenges = self._create_challenges()
        self._tag_challenges(challenges)
        self._create_records(challenges)
        self._create_social_app()

        self.stdout.write(self.style.SUCCESS("Data created successfully"))
