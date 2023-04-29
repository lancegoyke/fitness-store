import factory
from django.utils.text import slugify
from factory.django import DjangoModelFactory

from .models import Alternative, Category, Exercise


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

    @factory.post_generation
    def categories(self, create, extracted, **kwargs):
        if not create:
            # Simple build, do nothing.
            return

        if extracted:
            # A list of categories were passed in, use them
            for one_category in extracted:
                self.categories.add(one_category)


class AlternativeFactory(DjangoModelFactory):
    class Meta:
        model = Alternative

    original = factory.SubFactory(ExerciseFactory)
    alternate = factory.SubFactory(ExerciseFactory)
    problem = factory.Faker("sentence", nb_words=3, variable_nb_words=True)
