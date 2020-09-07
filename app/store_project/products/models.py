import itertools
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _

from django_lifecycle import (
    AFTER_CREATE,
    AFTER_DELETE,
    AFTER_UPDATE,
    hook,
    LifecycleModelMixin,
)
from markdownx.models import MarkdownxField
import stripe

from store_project.pages.models import Page


class Product(LifecycleModelMixin, models.Model):
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
    price = models.DecimalField(_("Price"), default=0, max_digits=10, decimal_places=2)
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

    @hook(AFTER_CREATE)
    def add_product_to_stripe(self):
        """
        Send basic product info to Stripe account.
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.Product.create(
            id=self.id, name=self.name, description=self.description, type="good"
        )
        stripe.Price.create(
            unit_amount_decimal=self.price * 100,
            currency="usd",
            product=self.id,
            lookup_key="current",
        )

    @hook(AFTER_UPDATE, when="name", has_changed=True)
    @hook(AFTER_UPDATE, when="description", has_changed=True)
    def update_product_in_stripe(self):
        """
        Update changed product info in Stripe.
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe.Product.modify(
                sid=str(self.id),
                name=self.name,
                description=self.description,
            )
        except:
            stripe.Product.create(
                id=self.id, name=self.name, description=self.description, type="good"
            )
            stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
                lookup_key="current",
            )

    @hook(AFTER_UPDATE, when="price", has_changed=True)
    def update_price_in_stripe(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        # stripe.Price.modify()
        try:
            stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
                lookup_key="current",
                transfer_lookup_key=True,
            )
        except:
            stripe.Product.create(
                id=self.id, name=self.name, description=self.description, type="good"
            )
            stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
                lookup_key="current",
                transfer_lookup_key=True,
            )

    @hook(AFTER_DELETE)
    def delete_product_and_price_in_stripe(self):
        """
        Mark Product and Price as inactive in Stripe. Keeping the item around
        in case it is needed in the future.
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.Product.modify(sid=str(self.id), active=False)
        stripe.Price.modify(sid=str(self.id), active=False)


class Program(Product):
    """A model for creating new programs. Extend Product model base functionality."""

    # equipment = models.ManyToManyField(Equipment, verbose_name=_("Required equipment"))
    duration = models.IntegerField(
        _("Number of weeks"),
        default=None,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    frequency = models.IntegerField(
        _("Training sessions per week"),
        default=None,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    program_file = models.FileField(_("File containing program"), null=True, blank=True)

    def get_absolute_url(self):
        """Get URL for product's detail view.

        Returns:
            str: URL for product detail.
        """
        return reverse("products:program_detail", kwargs={"slug": self.slug})
