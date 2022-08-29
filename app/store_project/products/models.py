import itertools
import logging
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.fields import GenericForeignKey
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

from store_project.pages.models import Page


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
    description = models.CharField(
        _("Short description of product"), max_length=255
    )
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
            logger.error("ERROR: Product and Price could not be marked inactive in Stripe.")


class Program(Product):
    """A model for creating new programs. Extend Product model base
    functionality."""

    # equipment = models.ManyToManyField(Equipment, verbose_name=_("Required equipment"))
    price = models.DecimalField(_("Price"), default=0, max_digits=10, decimal_places=2)
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
        blank=True
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
    price = models.DecimalField(_("Price"), default=0, max_digits=10, decimal_places=2)
    pdf = models.FileField(_("PDF"), upload_to="products/books/pdf/", max_length=100, blank=True)
    epub = models.FileField(_("EPUB"), upload_to="products/books/epub/", max_length=100, blank=True)
    mobi = models.FileField(_("MOBI"), upload_to="products/books/mobi/", max_length=100, blank=True)

    def get_absolute_url(self):
        return reverse('products:book_detail', kwargs={"slug": self.slug})

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


class PriceException(Exception):
    pass

class Currency(models.TextChoices):
    USD = ("USD", "USD")


class AggregateUsage(models.TextChoices):
    NULL = (None, "---------")
    SUM = ("sum", _("All usage during period"))
    LAST_DURING_PERIOD = ("last_during_period", _("Last usage record reported within a period"))
    LAST_EVER = ("last_ever", _("Last usage record ever (across period bounds)"))
    MAX = ("max", _("Maximum reported usage during a period"))

class Interval(models.TextChoices):
    NULL = (None, "---------")
    ONCE = "once"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"

class UsageType(models.TextChoices):
    NULL = (None, "---------")
    METERED = ("metered", _("`metered`: aggregates the total usage based on usage records"))
    LICENSED = ("licensed", _("`licensed`: automatically bills the `quantity` set when adding it to a subscription (default)"))

class PriceType(models.TextChoices):
    ONE_TIME = "one_time"
    RECURRING = "recurring"

class BillingScheme(models.TextChoices):
    TIERED = "tiered", _("`tiered`: unit pricing will be computed using a tiered strategy defined with `tiers` and `tiers_mode`")
    PER_UNIT = "per_unit", _("`per_unit`: the fixed amount will be charged per unit in `quantity` (for prices with `usage_type=licensed`) or per unit of total usage (for prices with `usage_type=metered`)")

class TaxBehavior(models.TextChoices):
    INCLUSIVE = ("inclusive", _("The price includes tax payment"))
    EXCLUSIVE = ("exclusive", _("The price does not include tax payment"))
    UNSPECIFIED = ("unspecified", _("Tax behavior not specified"))

class TiersMode(models.TextChoices):
    NULL = (None, "---------")
    GRADUATED = ("graduated", _("`graduated`: pricing changes as the quantity grows"))
    VOLUME = ("volume", _("`volume`: the maximum quantity within a period determines the per unit price"))


class Price(LifecycleModelMixin, models.Model):
    """Price objects tell us how to bill for products."""

    # recreate the Stripe Price object
    # https://stripe.com/docs/api/prices/object

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    active = models.BooleanField(default=True)
    currency = models.CharField(max_length=3, choices=Currency.choices, default=Currency.USD)
    metadata = models.JSONField(blank=True, default=list)
    nickname = models.CharField(max_length=200, help_text=_("A brief description of the price, hidden from customers"), blank=True)
    aggregate_usage = models.CharField(max_length=18, choices=AggregateUsage.choices, help_text="Specifices a usage aggregation strategy for prices of `usage_type=metered`", null=True, default=None)
    interval = models.CharField(max_length=5, choices=Interval.choices, null=True, default=None)
    interval_count = models.PositiveSmallIntegerField(help_text=_("The number of intervals between subscription billings, e.g., `interval=month` and `interval_count=3` bills every 3 months"), null=True, default=None)
    usage_type = models.CharField(max_length=8, choices=UsageType.choices, null=True, default=None)
    price_type = models.CharField(max_length=9, choices=PriceType.choices, default=PriceType.ONE_TIME)
    unit_amount = models.PositiveIntegerField(help_text=_("Amount in cents; only set if `billing_scheme=per_unit`"), null=True, default=None)
    billing_scheme = models.CharField(max_length=8, help_text=_("How to compute the price per period"), choices=BillingScheme.choices, default=BillingScheme.PER_UNIT)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    tax_behavior = models.CharField(max_length=11, choices=TaxBehavior.choices, default=TaxBehavior.UNSPECIFIED)
    tiers_mode = models.CharField(max_length=9, choices=TiersMode.choices, null=True, default=None)
    # omitted `currency_options`
    # omitted `custom_unit_amount`
    # omitted `product_data`
    # omitted `livemode`
    # omitted `lookup_key`
    # omitted `transform_quantity` with `divide_by` and `round`
    # omitted `unit_amount_decimal`
    
    # This product will be a descendent of the Product abstract base class
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True)
    object_id = models.CharField(max_length=50, null=True)
    product = GenericForeignKey('content_type', 'object_id')

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def check_for_exceptions(self):
        """
        See if the combination of data is coherent
        """
        if self.aggregate_usage and self.usage_type is not UsageType.METERED:
            raise PriceException("You can only specify `aggregate_usage` for plans with `usage_type=metered`")

        if Tier.objects.filter(price_obj=self.id) and self.billing_scheme is not BillingScheme.TIERED:
            raise PriceException("You shouldn't have Tiers for prices without `billing_scheme=tiered`")

        if Tier.objects.filter(price_obj=self.id) and self.unit_amount:
            raise PriceException("If `billing_scheme=tiered`, a fixed `unit_amount` is not supported.")

        if self.billing_scheme == BillingScheme.PER_UNIT and not self.unit_amount:
            raise PriceException("If `billing_scheme=per_unit`, then `unit_amount` is required")

        if self.billing_scheme == BillingScheme.PER_UNIT and (self.tiers_mode or Tier.objects.filter(price_obj=self.id)):
            raise PriceException("If `billing_scheme=per_unit`, then a tiered configuration is not supported")

        if self.interval == Interval.YEAR and self.interval_count > 1:
            raise PriceException("The `interval_count` for yearly prices can't be greater than 1")

        if self.interval == Interval.MONTH and self.interval_count > 12:
            raise PriceException("The `interval_count` for monthly prices can't be greater than 12")

        if self.interval == Interval.WEEK and self.interval_count > 52:
            raise PriceException("The `interval_count` for weekly prices can't be greater than 52")

        if self.interval == Interval.DAY and self.interval_count > 365:
            raise PriceException("The `interval_count` for daily prices can't be greater than 365")

        pass


    @hook(AFTER_CREATE, when="billing_scheme", is_now=BillingScheme.PER_UNIT)
    def add_price_to_stripe(self):
        """
        Create this Price object in Stripe if should be billed per unit
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        data = {
            "unit_amount": self.unit_amount,
            "currency": self.currency,
            "product": self.product.id,
            "metadata": self.metadata,
            "nickname": self.nickname,
            "recurring": {
                "interval": self.interval,
                "interval_count": self.interval_count,
                "aggregate_usage": self.aggregate_usage,
                "usage_type": self.usage_type,
            },
            "billing_scheme": self.billing_scheme,
            "tax_behavior": self.tax_behavior,
        }
        price = stripe.Price.create(**data)


class Tier(models.Model):
    """Tiers describe graduated pricing"""

    class AmountType(models.TextChoices):
        FLAT_AMOUNT = ("flat_amount", _("flat_amount: price for the entire tier"))
        UNIT_AMOUNT = ("unit_amount", _("unit_amount: per unit price for units relevant to the tier"))

    amount_type = models.CharField(max_length=19, choices=AmountType.choices)
    amount = models.PositiveIntegerField()
    up_to = models.PositiveIntegerField()  # 0 will denote fallback tier of "inf"
    price_obj = models.ForeignKey(Price, on_delete=models.CASCADE, related_name="tiers")

    # @hook(AFTER_CREATE, when="price_obj")
    def add_price_to_stripe(self):
        """
        TODO
        Create the related Price object in Stripe if it has tiered billing
        """
        stripe.api_key = settings.STRIPE_SECRET_KEY
        # tiers = Tier.objects.filter(price_obj=self)
        data = {
            "unit_amount": self.unit_amount,
            "currency": self.currency,
            "product": self.product.id,
            "metadata": self.metadata,
            "nickname": self.nickname,
            "recurring": {
                "interval": self.interval,
                "interval_count": self.interval_count,
                "aggregate_usage": self.aggregate_usage,
                "usage_type": self.usage_type,
            },
            "tiers": tiers,
            "tiers_mode": self.tiers_mode,
            "billing_scheme": self.billing_scheme,
            "tax_behavior": self.tax_behavior,
        }
        price = stripe.Price.create(**data)


class Subscription(Product):
    """A product with a recurring bill."""

    def get_absolute_url(self):
        return reverse('products:subscription_detail', kwargs={"slug": self.slug})

    @hook(AFTER_CREATE)
    def add_subscription_permission(self):
        """
        Create a permission for users who have access to this subscription and add it
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
    def remove_subscription_permission(self):
        """
        Remove the can_view_{book.slug} permission for associated book.
        """
        permission = Permission.objects.get(
            codename=f"can_view_{self.slug}",
            name=f"Can view {self.name}",
            content_type=ContentType.objects.get_for_model(Book),
        ).delete()
        logger.info(f"Permission {permission} deleted.")

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
            currency="usd",
            billing_scheme="tiered",
            tiers=[
                {"flat_amount": 1000, "up_to": 2},
                {"flat_amount": 1500, "up_to": 5},
                {"flat_amount": 2000, "up_to": "inf"},
            ],
            tiers_mode="volume",
            recurring={"interval": self.billing_interval},
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
            logger.error("ERROR: Product and Price could not be marked inactive in Stripe.")
