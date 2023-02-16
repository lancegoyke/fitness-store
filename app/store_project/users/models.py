import uuid

from django.contrib.auth.models import AbstractUser
from django.db.models import CharField, UUIDField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Default user for store_project."""

    id = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)
    stripe_customer_id = CharField(_("Stripe Customer ID"), max_length=100, blank=True)

    def __str__(self):
        return self.email

    def get_absolute_url(self):
        """Get url for user's profile view."""
        return reverse("users:profile")
