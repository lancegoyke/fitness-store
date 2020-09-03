import itertools
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _

from markdownx.models import MarkdownxField

from store_project.pages.models import Page


class Product(models.Model):
    """An abstract base class model for creating new products."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        _("Name of product"), max_length=settings.PRODUCT_NAME_MAX_LENGTH
    )
    slug = models.SlugField(
        _("Slug for product"),
        default="",
        null=False,
        unique=True,
        max_length=settings.PRODUCT_NAME_MAX_LENGTH,
    )
    description = models.CharField(
        _("Short description of product"), blank=True, max_length=255
    )
    views = models.PositiveIntegerField(_("Number of times viewed"), default=0)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    author = models.ForeignKey(
        get_user_model(),
        verbose_name=_("Author of product"),
        null=True,
        on_delete=models.SET_NULL,
    )
    featured_image = models.ImageField(
        _("Featured product image"),
        upload_to="products/images/",
        blank=True,
    )
    page_content = MarkdownxField(
        _("Page content, in markdown"), default="", blank=True
    )

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class Program(Product):
    """A model for creating new programs. Extend Product model base functionality."""

    # equipment = models.ManyToManyField(Equipment, verbose_name=_("Required equipment"))
    program_file = models.FileField(_("File containing program"), null=True, blank=True)

    def get_absolute_url(self):
        """Get URL for product's detail view.

        Returns:
            str: URL for product detail.
        """
        return reverse("products:program_detail", kwargs={"slug": self.slug})
