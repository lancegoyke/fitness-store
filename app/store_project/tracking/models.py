import uuid

from django.db import models
from django.utils.translation import gettext as _


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


class Athlete(models.Model):
    """
    The person performing the tests
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # user = models.ForeignKey(User, on_delete=models.CASCADE)


class Result(models.Model):
    """
    The test result
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scheduled = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(null=True, default=None)
    athlete = models.ForeignKey("Athlete", on_delete=models.SET_NULL, null=True, default=None)
    exercise = models.ForeignKey("exercises.Exercise", on_delete=models.CASCADE)
    reps = models.IntegerField(help_text="Number of reps", blank=True)
    weight = models.IntegerField(help_text="Weight in grams", blank=True)
    height = models.IntegerField(help_text="Height in millimeters", blank=True)
    distance = models.IntegerField(help_text="Distance in millimeters", blank=True)
    duration = models.DurationField(blank=True)
    notes = models.TextField(blank=True)


class Session(models.Model):
    """
    A collection of tests to be performed together
    """

    # id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # athlete = models.ForeignKey("Athlete", on_delete=models.SET_NULL, null=True, default=None)
    # exercises = models.CharField(max_length=1000)
    pass
