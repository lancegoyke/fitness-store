try:
    import factory
    from django.utils.text import slugify
    from factory.django import DjangoModelFactory

    from store_project.users.factories import SuperAdminFactory

    from .models import Page

    class PageFactory(DjangoModelFactory):
        class Meta:
            model = Page

        title = "About"
        content = factory.Faker("paragraph", nb_sentences=5, variable_nb_sentences=True)
        slug = factory.LazyAttribute(lambda o: slugify(o.title))
        author = factory.SubFactory(SuperAdminFactory)

except ImportError:
    # Factory Boy is not available (likely in production)
    # Define dummy classes to prevent import errors
    class PageFactory:
        pass
