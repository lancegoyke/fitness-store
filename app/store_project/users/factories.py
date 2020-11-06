import factory
from factory.django import DjangoModelFactory

from .models import User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    name = factory.Faker("name")
    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda obj: f"{obj.username}@example.com")


class SuperAdminFactory(UserFactory):
    is_staff = True
    is_superuser = True
    name = "Lance Goyke"
    email = "lance@lancegoyke.com"
