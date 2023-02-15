from django.contrib.syndication.views import Feed
from django.utils.translation import gettext_lazy as _

from store_project.products.models import Program


class LatestProductsFeed(Feed):
    title = _("Mastering Fitness Products Feed")
    link = "/"
    description = _("Updates on changes and additions to Mastering Fitness.")

    def items(self):
        return Program.objects.filter(status=Program.PUBLIC).order_by("-created")[:5]

    def item_title(self, item):
        """Title of the product."""
        return _(item.name)

    def item_description(self, item):
        """Short description of the product."""
        return _(item.description)

    def item_author_name(self, item):
        """Different products may have different authors."""
        return _(item.author.name)

    def item_categories(self, item):
        """Categories this product belongs to."""
        return item.categories.all()
