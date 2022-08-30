from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext as _

from store_project.users.models import User


class Category(models.Model):
    """
    Categories meant to describe fitness tests
    """
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = _("categories")

    def __str__(self):
        return self.name


class Test(models.Model):
    """
    Fitness tests
    """
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(null=True, default=None)
    video_link = models.URLField(blank=True, default=None)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    author = models.ForeignKey(
        User,
        verbose_name=_("Author of product"),
        null=True,
        on_delete=models.SET_NULL,
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_DEFAULT,
        null=True,
        default=None,
    )

    def __str__(self):
        return f"{self.name}"


class UnitsOfMeasurement(models.TextChoices):
    SECONDS = "sec", _("Seconds")
    POUNDS = "lb", _("Pounds")
    KILOS = "kg", _("Kilograms")
    MILES = "mi", _("Miles")
    METERS = "m", _("Meters")
    FEET = "ft", _("Feet")
    INCHES = "in", _("Inches")
    YARDS = "yd", _("Yards")
    WATTS = "W", _("Watts")


class Measure(models.Model):
    """
    A point of performance occuring at a particular time and body
    """
    value = models.PositiveIntegerField()
    unit = models.CharField(max_length=3, choices=UnitsOfMeasurement.choices)
    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    
    def __str__(self):
        return f"{self.test} for {self.user} - {self.value} {self.unit}"
