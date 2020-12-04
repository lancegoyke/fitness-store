import uuid

from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _


class Category(models.Model):
    """The kind of exercise. Ex: squat, single leg, hinge, etc."""

    id = models.UUIDField(
        _("Category ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(_("Category name"), max_length=30)
    slug = models.SlugField(
        _("Slug for category"),
        default="",
        null=False,
        unique=True,
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Category"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Exercise(models.Model):
    """An exercise with video links to demonstrate and explain the movement."""

    id = models.UUIDField(
        _("Exercise ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(_("Exercise name"), max_length=200)
    slug = models.SlugField(
        _("Slug for exercise"),
        default="",
        null=False,
        unique=True,
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    demonstration = models.URLField(
        _("Demonstration link"), max_length=200, default="", blank=True
    )
    explanation = models.URLField(
        _("Explanation link"), max_length=200, default="", blank=True
    )
    category = models.ManyToManyField(
        "Category",
        verbose_name=_("Exercise category"),
        blank=True,
    )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("exercises:detail", kwargs={"slug": self.slug})


class Alternative(models.Model):
    """An alternative exercise."""

    id = models.UUIDField(
        _("Alternative ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    original = models.ForeignKey(
        "Exercise",
        verbose_name=_("Original exercise"),
        related_name="original",
        on_delete=models.CASCADE,
    )
    alternate = models.ForeignKey(
        "Exercise",
        verbose_name=_("Alternative exercise"),
        related_name="alternate",
        on_delete=models.CASCADE,
    )
    problem = models.CharField(
        _("Problem with original exercise"), max_length=200, default="", blank=True
    )
