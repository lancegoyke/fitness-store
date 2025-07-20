from django.db import models
from django.urls import reverse
from taggit.managers import TaggableManager
import statistics


class DifficultyLevel(models.TextChoices):
    BEGINNER = "beginner", "Beginner"
    INTERMEDIATE = "intermediate", "Intermediate"
    ADVANCED = "advanced", "Advanced"


class Challenge(models.Model):
    """The exercise challenges presented to clients."""

    name = models.CharField(max_length=200)
    description = models.TextField()
    summary = models.CharField(
        max_length=300,
        blank=True,
        help_text="Brief description for list view",
    )
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    date_created = models.DateTimeField(
        auto_now_add=True,
        editable=False,
    )
    difficulty_level = models.CharField(
        max_length=20,
        choices=DifficultyLevel,
        default=DifficultyLevel.BEGINNER,
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

    @property
    def difficulty_color(self) -> str:
        """Return Bulma color name for the difficulty indicator."""
        mapping = {
            DifficultyLevel.BEGINNER: "success",
            DifficultyLevel.INTERMEDIATE: "warning",
            DifficultyLevel.ADVANCED: "danger",
        }
        return mapping.get(self.difficulty_level, "info")

    @property
    def estimated_completion_time(self):
        """Return the median completion time for all records of this challenge."""
        time_scores = self.records.values_list('time_score', flat=True)
        if not time_scores:
            return None
        
        # Convert to total seconds for calculation
        time_seconds = [score.total_seconds() for score in time_scores]
        median_seconds = statistics.median(time_seconds)
        
        # Round to nearest second and convert back to a timedelta
        from datetime import timedelta
        return timedelta(seconds=round(median_seconds))


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
    user = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True)

    class Meta:
        """Meta definition for Record."""

        verbose_name = "Record"
        verbose_name_plural = "Records"

    def __str__(self):
        """Unicode representation of Record."""
        return f"{self.challenge.name} {self.date_recorded}"
