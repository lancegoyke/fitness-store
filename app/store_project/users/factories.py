from django.contrib.auth.hashers import make_password

import factory
from factory.django import DjangoModelFactory

from .models import User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = (
            "username",
            "email",
        )

    name = factory.Faker("name")
    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda obj: f"{obj.username}@example.com")
    password = factory.LazyFunction(lambda: make_password("testpass123"))

    # @factory.post_generation
    # def password(self, create: bool, extracted: Sequence[Any], **kwargs):
    #     password = extracted if extracted else make_password("testpass123")
    #     self.set_password(password)


class SuperAdminFactory(UserFactory):
    name = "Lance Goyke"
    username = "lance"
    email = "lance@lancegoyke.com"
    is_staff = True
    is_superuser = True
