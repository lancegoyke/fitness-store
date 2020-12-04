from django.utils.text import slugify

import factory
from factory.django import DjangoModelFactory

from .models import Category, Exercise


class CategoryFactory(DjangoModelFactory):
    class Meta:
        model = Category

    name = factory.sequence(lambda n: f"Category{n}")
    slug = factory.LazyAttribute(lambda o: slugify(o.name))


class ExerciseFactory(DjangoModelFactory):
    class Meta:
        model = Exercise

    name = factory.Faker("sentence", nb_words=3, variable_nb_words=True)
    slug = factory.LazyAttribute(lambda o: slugify(o.name))
    demonstration = "https://youtu.be/5DQgXXkNMOk"
    explanation = "https://www.youtube.com/watch?v=7NCF7hS3CCE"
