from decimal import Decimal
import random

from django.utils.text import slugify

import factory
from factory.django import DjangoModelFactory

from store_project.users.factories import UserFactory, SuperAdminFactory
from store_project.products import models


class BookFactory(DjangoModelFactory):
    class Meta:
        model = models.Book

    name = factory.Faker("sentence", nb_words=5, variable_nb_words=True)
    description = factory.Faker("paragraph", nb_sentences=2, variable_nb_sentences=True)
    slug = factory.LazyAttribute(lambda o: slugify(o.name))
    status = models.Product.PUBLIC
    price = 10.00
    author = factory.SubFactory(SuperAdminFactory)
    page_content = factory.Faker(
        "paragraph", nb_sentences=5, variable_nb_sentences=True
    )


class ProgramFactory(DjangoModelFactory):
    class Meta:
        model = models.Program

    name = factory.Faker("sentence", nb_words=5, variable_nb_words=True)
    description = factory.Faker("paragraph", nb_sentences=2, variable_nb_sentences=True)
    slug = factory.LazyAttribute(lambda o: slugify(o.name))
    status = models.Product.PUBLIC
    price = 10.00
    author = factory.SubFactory(SuperAdminFactory)
    page_content = factory.Faker(
        "paragraph", nb_sentences=5, variable_nb_sentences=True
    )
    duration = factory.LazyFunction(lambda: random.randint(2, 8))
    frequency = factory.LazyFunction(lambda: random.randint(3, 7))


class PriceFixedUnitFactory(DjangoModelFactory):
    class Meta:
        model = models.Price

    unit_amount = 1199
    price_type = models.PriceType.ONE_TIME
    product = factory.SubFactory(ProgramFactory)


class PriceFixedMonthlySubscriptionFactory(DjangoModelFactory):
    class Meta:
        model = models.Price

    unit_amount = 499
    price_type = models.PriceType.RECURRING
    interval = models.Interval.MONTH
    product = factory.SubFactory(ProgramFactory)
