from typing import Dict, List
import requests

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from store_project.meals import models


class Command(BaseCommand):
    help = "Adds Units of Measurement to database"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        # Blank slate
        models.Unit.objects.all().delete()

        # Get the json data from ESHA Nutrition API
        endpoint = "https://nutrition-api.esha.com/units"
        headers = {
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": settings.ESHA_SUB_KEY,
        }
        r = requests.get(endpoint, headers=headers)
        data: List[Dict] = r.json()

        count = 0
        for d in data:
            models.Unit.objects.create(
                id=d["id"],
                description=d["description"],
                abbr=d["abbr"],
            )
            count = count + 1
        self.stdout.write(f"Added {count} units of measurement...")
