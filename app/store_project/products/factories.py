try:
    import random

    import factory
    from django.utils.text import slugify
    from factory.django import DjangoModelFactory

    from store_project.users.factories import SuperAdminFactory

    from .models import Book
    from .models import Product
    from .models import Program

    class BookFactory(DjangoModelFactory):
        class Meta:
            model = Book

        name = factory.Faker("sentence", nb_words=5, variable_nb_words=True)
        description = factory.Faker(
            "paragraph", nb_sentences=2, variable_nb_sentences=True
        )
        slug = factory.LazyAttribute(lambda o: slugify(o.name))
        status = Product.PUBLIC
        price = 10.00
        author = factory.SubFactory(SuperAdminFactory)
        page_content = factory.Faker(
            "paragraph", nb_sentences=5, variable_nb_sentences=True
        )

    class ProgramFactory(DjangoModelFactory):
        class Meta:
            model = Program

        name = factory.Faker("sentence", nb_words=5, variable_nb_words=True)
        description = factory.Faker(
            "paragraph", nb_sentences=2, variable_nb_sentences=True
        )
        slug = factory.LazyAttribute(lambda o: slugify(o.name))
        status = Product.PUBLIC
        price = 10.00
        author = factory.SubFactory(SuperAdminFactory)
        page_content = factory.Faker(
            "paragraph", nb_sentences=5, variable_nb_sentences=True
        )
        duration = factory.LazyFunction(lambda: random.randint(2, 8))
        frequency = factory.LazyFunction(lambda: random.randint(3, 7))

except ImportError:
    # Factory Boy is not available (likely in production)
    # Define dummy classes to prevent import errors
    class BookFactory:
        pass

    class ProgramFactory:
        pass
