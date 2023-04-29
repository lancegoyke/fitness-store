import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from django_lifecycle import (
    BEFORE_CREATE,
    AFTER_CREATE,
    BEFORE_DELETE,
    BEFORE_UPDATE,
    AFTER_UPDATE,
    hook,
    LifecycleModelMixin,
)
from markdownx.models import MarkdownxField
import stripe


User = get_user_model()
logger = logging.getLogger(__name__)


class Category(models.Model):
    name = models.CharField(max_length=30)

    class Meta:
        ordering = ["name"]
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Product(LifecycleModelMixin, models.Model):
    """An abstract base class model for creating new products."""

    PUBLIC = "pb"
    PRIVATE = "pr"
    DRAFT = "dr"
    STATUS_CHOICES = [
        (PUBLIC, "Public"),
        (PRIVATE, "Private"),
        (DRAFT, "Draft"),
    ]
    status = models.CharField(
        max_length=2,
        choices=STATUS_CHOICES,
        default=DRAFT,
    )

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
    description = models.CharField(_("Short description of product"), max_length=255)
    price = models.DecimalField(_("Price"), default=0, max_digits=10, decimal_places=2)
    stripe_price_id = models.CharField(_("Stripe Price ID"), max_length=100, blank=True)
    views = models.PositiveIntegerField(_("Number of times viewed"), default=0)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    author = models.ForeignKey(
        User,
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
        ordering = ["-created"]

    def __str__(self):
        return self.name

    def is_public(self):
        return self.status in {self.PUBLIC}

    @hook(BEFORE_CREATE)
    def add_product_to_stripe(self):
        """
        Send basic product info to Stripe account.
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        product = stripe.Product.create(
            id=self.id,
            name=self.name,
            description=self.description,
            type="good",
        )
        price = stripe.Price.create(
            unit_amount_decimal=self.price * 100,
            currency="usd",
            product=self.id,
        )
        self.stripe_price_id = price.id  # save to database for easy reference
        logger.info(f"Product {product} added to Stripe.")
        logger.info(f"Price {price} added to Stripe.")

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
            logger.info(f"Product modified: id={self.id}")
        except stripe.error.InvalidRequestError as e:
            logger.error("ERROR: Product could not be modified.")
            logger.error("ERROR: Creating Product and Price instead.")
            logger.error(f"ERROR: {e}")
            stripe.Product.create(
                id=self.id,
                name=self.name,
                description=self.description,
                type="good",
            )
            stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
            )

    @hook(BEFORE_UPDATE, when="price", has_changed=True)
    def update_price_in_stripe(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            old_price = stripe.Price.modify(self.stripe_price_id, active=False)
            new_price = stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
            )
            logger.info(f"Old price: {old_price}")
            logger.info(f"New price: {new_price}")
            self.stripe_price_id = new_price.id
        except stripe.error.InvalidRequestError as e:
            logger.error("ERROR: Price could not be modified.")
            logger.error("ERROR: Creating Product and Price instead.")
            logger.error(f"ERROR: {e}")
            stripe.Product.create(
                id=self.id, name=self.name, description=self.description, type="good"
            )
            price = stripe.Price.create(
                unit_amount_decimal=self.price * 100,
                currency="usd",
                product=self.id,
            )
            self.stripe_price_id = price.id

    @hook(BEFORE_DELETE)
    def delete_product_and_price_in_stripe(self):
        """
        Mark Product and Price as inactive in Stripe. Keeping the item around
        in case it is needed in the future.
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            product = stripe.Product.modify(sid=str(self.id), active=False)
            price = stripe.Price.modify(sid=str(self.id), active=False)
            logger.info(f"Product {product} has been marked inactive in Stripe.")
            logger.info(f"Price {price} has been marked inactive in Stripe.")
        except stripe.error.InvalidRequestError as e:
            logger.error(f"ERROR: {e}")
            logger.error(
                "ERROR: Product and Price could not be marked inactive in Stripe."
            )


class Program(Product):
    """A model for creating new programs. Extend Product model base
    functionality."""

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
    categories = models.ManyToManyField(Category, blank=True)
    program_file = models.FileField(
        _("File containing program"),
        upload_to="products/programs/",
        null=True,
        blank=True,
    )

    def get_absolute_url(self):
        """Get URL for product's detail view.

        Returns:
            str: URL for product detail.
        """
        return reverse("products:program_detail", kwargs={"slug": self.slug})

    @hook(AFTER_CREATE)
    def add_program_permission(self):
        """
        Create a permission for users who have access to this program and add it
        to the "comped" group.
        """
        permission = Permission.objects.create(
            codename=f"can_view_{self.slug}",
            name=f"Can view {self.name}",
            content_type=ContentType.objects.get_for_model(Program),
        )
        logger.info(f"Permission {permission} created.")
        comped_group, created = Group.objects.get_or_create(name="comped")
        comped_group.permissions.add(permission)
        logger.info(f"Permission {permission} added to comped_group.")

    @hook(BEFORE_DELETE)
    def remove_program_permission(self):
        """
        Remove the can_view_{program.slug} permission for associated program.
        """
        permission = Permission.objects.get(
            codename=f"can_view_{self.slug}",
            name=f"Can view {self.name}",
            content_type=ContentType.objects.get_for_model(Program),
        ).delete()
        logger.info(f"Permission {permission} deleted.")


class Book(Product):
    """A model for digital books. Extend Product model base functionality."""

    # self.name will contain title and subtitle
    pdf = models.FileField(
        _("PDF"), upload_to="products/books/pdf/", max_length=100, blank=True
    )
    epub = models.FileField(
        _("EPUB"), upload_to="products/books/epub/", max_length=100, blank=True
    )
    mobi = models.FileField(
        _("MOBI"), upload_to="products/books/mobi/", max_length=100, blank=True
    )

    def get_absolute_url(self):
        return reverse("products:book_detail", kwargs={"slug": self.slug})

    @hook(AFTER_CREATE)
    def add_book_permission(self):
        """
        Create a permission for users who have access to this book and add it
        to the "comped" group.
        """
        permission = Permission.objects.create(
            codename=f"can_view_{self.slug}",
            name=f"Can view {self.name}",
            content_type=ContentType.objects.get_for_model(Book),
        )
        logger.info(f"Permission {permission} created.")
        comped_group, created = Group.objects.get_or_create(name="comped")
        comped_group.permissions.add(permission)
        logger.info(f"Permission {permission} added to comped_group.")

    @hook(BEFORE_DELETE)
    def remove_book_permission(self):
        """
        Remove the can_view_{book.slug} permission for associated book.
        """
        permission = Permission.objects.get(
            codename=f"can_view_{self.slug}",
            name=f"Can view {self.name}",
            content_type=ContentType.objects.get_for_model(Book),
        ).delete()
        logger.info(f"Permission {permission} deleted.")
