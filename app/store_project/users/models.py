from django.contrib.auth.models import AbstractUser
from django.db.models import CharField, EmailField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Default user for store_project."""

    #: First and last name do not cover name patterns around the globe
    username = None
    name = CharField(_("Name of User"), blank=True, max_length=255)
    email = EmailField(_("Email Address"), unique=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email

    def get_absolute_url(self):
        """Get url for user's detail view.

        Returns:
            str: URL for user detail.
        """
        return reverse("users:detail", kwargs={"username": self.username})
