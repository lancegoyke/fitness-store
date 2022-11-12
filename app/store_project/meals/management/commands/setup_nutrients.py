from typing import Dict, List
import requests

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from store_project.meals import models


class Command(BaseCommand):
    help = "Adds Nutrients to database"

    @transaction.atomic
    def handle(self, *args, **kwargs):
        # Blank slate
        models.Nutrient.objects.all().delete()

        # Get the json data from ESHA Nutrition API
        endpoint = "https://nutrition-api.esha.com/nutrients"
        headers = {
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": settings.ESHA_SUB_KEY,
        }
        r = requests.get(endpoint, headers=headers)
        data: List[Dict] = r.json()

        count = 0
        for d in data:
            models.Nutrient.objects.create(
                id=d["id"],
                description=d["description"],
                unit=d.get("unit", None),
                unit_id=d.get("unitId", None),
            )
            count = count + 1
        self.stdout.write(f"Added {count} nutrients...")
