from autoslug import AutoSlugField
from django.contrib.auth import get_user_model
from django.db import models
from django.urls import reverse
from taggit.managers import TaggableManager


# Create your models here.
class Challenge(models.Model):
    """The exercise challenges presented to clients."""

    name = models.CharField(max_length=200)
    description = models.TextField()
    slug = AutoSlugField(populate_from="name")
    date_created = models.DateTimeField(
        auto_now_add=True,
        editable=False,
    )
    tags = TaggableManager()

    class Meta:
        """Meta definition for Challenge."""

        verbose_name = "Challenge"
        verbose_name_plural = "Challenges"

    def __str__(self):
        """Unicode representation of Challenge."""
        return self.name

    def get_absolute_url(self):
        return reverse("challenge_detail", kwargs={"slug": self.slug})


class Record(models.Model):
    """The score someone gets on a workout challenge."""

    challenge = models.ForeignKey(
        Challenge, on_delete=models.CASCADE, related_name="records"
    )
    time_score = models.DurationField(
        help_text="How long did it take you? HH:MM:SS",
    )
    notes = models.CharField(max_length=200, blank=True)
    date_recorded = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(get_user_model(), on_delete=models.SET_NULL, null=True)

    class Meta:
        """Meta definition for Record."""

        verbose_name = "Record"
        verbose_name_plural = "Records"

    def __str__(self):
        """Unicode representation of Record."""
        return f"{self.challenge.name} {self.date_recorded}"
