import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Default user for store_project."""

    class Sex(models.TextChoices):
        UNKNOWN = "U", _("Prefer not to say")
        MALE = "M", _("Male")
        FEMALE = "F", _("Female")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    birthday = models.DateField(
        _("Birthday"),
        help_text="Please use the following format: YYYY-MM-DD",
        null=True,
        blank=True,
    )
    name = models.CharField(_("Name of User"), blank=True, max_length=255)
    points = models.IntegerField(
        _("Points"), default=0, validators=[MinValueValidator(0)]
    )
    sex = models.CharField(
        _("Sex"), max_length=1, choices=Sex.choices, default=Sex.UNKNOWN
    )
    stripe_customer_id = models.CharField(
        _("Stripe Customer ID"), max_length=100, blank=True
    )

    def __str__(self):
        return self.email

    def get_absolute_url(self):
        """Get url for user's profile view."""
        return reverse("users:profile")
