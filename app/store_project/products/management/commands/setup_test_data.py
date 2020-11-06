import random

from django.core.management.base import BaseCommand
from django.db import transaction

from store_project.products.models import Program
from store_project.products.factories import ProgramFactory
from store_project.users.models import User
from store_project.users.factories import UserFactory, SuperAdminFactory


NUM_PROGRAMS = 35
NUM_USERS = 10


class Command(BaseCommand):
    help = "Generates test products"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        self.stdout.write("Deleting old data...")
        models = [User, Program]
        for m in models:
            m.objects.all().delete()

        self.stdout.write("Creating new data...")
        # Create the superuser
        superuser = SuperAdminFactory()

        # Create the other users
        people = []
        for _ in range(NUM_USERS):
            person = UserFactory()
            people.append(person)

        # Create the programs
        for _ in range(NUM_PROGRAMS):
            ProgramFactory()
