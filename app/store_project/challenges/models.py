import re
import statistics
import uuid

from django.db import models
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _


class ChallengeTag(models.Model):
    """Tags for categorizing challenges."""

    id = models.UUIDField(
        _("Challenge Tag ID"), primary_key=True, default=uuid.uuid4, editable=False
    )
    name = models.CharField(_("Tag name"), max_length=50)
    slug = models.SlugField(
        _("Slug for tag"),
        default="",
        null=False,
        unique=True,
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Challenge Tag"
        verbose_name_plural = "Challenge Tags"

    def __str__(self):
        return self.name


class DifficultyLevel(models.TextChoices):
    BEGINNER = "beginner", "Beginner"
    INTERMEDIATE = "intermediate", "Intermediate"
    ADVANCED = "advanced", "Advanced"


# Module-level constants to eliminate duplication
DIFFICULTY_ORDER = {
    DifficultyLevel.BEGINNER: 0,
    DifficultyLevel.INTERMEDIATE: 1,
    DifficultyLevel.ADVANCED: 2,
}

DIFFICULTY_COLOR_MAPPING = {
    DifficultyLevel.BEGINNER: "success",
    DifficultyLevel.INTERMEDIATE: "warning",
    DifficultyLevel.ADVANCED: "danger",
}

# Regex patterns for challenge variations (L1, L2, etc.)
VARIATION_SUFFIX_PATTERN = r"\s*\(L\d+\)$"
VARIATION_NUMBER_PATTERN = r"\(L(\d+)\)$"


class ChallengeQuerySet(models.QuerySet):
    def grouped(self):
        """Group challenges by their base name (removing L1, L2, etc. suffixes)."""
        grouped: dict[str, list["Challenge"]] = {}

        # Preserve the existing ordering instead of forcing date_created ordering
        for challenge in self:
            base_name = challenge.base_name
            grouped.setdefault(base_name, []).append(challenge)

        # Sort each group by difficulty level (beginner first, then intermediate, then advanced)
        for challenges in grouped.values():
            challenges.sort(key=lambda c: DIFFICULTY_ORDER.get(c.difficulty_level, 99))

        return grouped

    def with_completion_stats(self):
        """Annotate challenges with completion time statistics."""
        from django.db.models import Avg
        from django.db.models import Count

        return self.annotate(
            record_count=Count("records"),
            avg_completion_time=Avg("records__time_score"),
        )


class ChallengeManager(models.Manager):
    def get_queryset(self):
        return ChallengeQuerySet(self.model, using=self._db)

    def grouped(self):
        """Convenience proxy for `queryset.grouped()`."""
        return self.get_queryset().grouped()

    def with_completion_stats(self):
        """Convenience proxy for `queryset.with_completion_stats()`."""
        return self.get_queryset().with_completion_stats()


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
    challenge_tags = models.ManyToManyField(
        "ChallengeTag",
        verbose_name=_("Challenge tags"),
        blank=True,
    )

    objects = ChallengeManager()

    class Meta:
        """Meta definition for Challenge."""

        verbose_name = "Challenge"
        verbose_name_plural = "Challenges"
        indexes = [
            models.Index(fields=["difficulty_level"]),
            models.Index(fields=["date_created"]),
        ]

    def __str__(self):
        """Unicode representation of Challenge."""
        return self.name

    def get_absolute_url(self):
        return reverse("challenges:challenge_detail", kwargs={"slug": self.slug})

    @cached_property
    def base_name(self):
        """Extract base name by removing (L1), (L2), etc. suffixes."""
        # Remove pattern like " (L1)", " (L2)", etc.
        base_name = re.sub(VARIATION_SUFFIX_PATTERN, "", self.name)
        return base_name.strip()

    @cached_property
    def variation_number(self):
        """Extract the variation number from names like 'Fit Fall (L1)', returns None if no variation."""
        match = re.search(VARIATION_NUMBER_PATTERN, self.name)
        return int(match[1]) if match else None

    def is_variation(self):
        """Check if this challenge is a variation (has L1, L2, etc. suffix)."""
        return self.variation_number is not None

    @property
    def difficulty_color(self) -> str:
        """Return Bulma color name for the difficulty indicator."""
        return DIFFICULTY_COLOR_MAPPING.get(self.difficulty_level, "info")

    @property
    def estimated_completion_time(self):
        """Return the median completion time for all records of this challenge."""
        time_scores = self.records.values_list("time_score", flat=True)
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
        indexes = [
            models.Index(fields=["date_recorded"]),
            models.Index(fields=["time_score"]),
            models.Index(fields=["challenge", "date_recorded"]),
        ]

    def __str__(self):
        """Unicode representation of Record."""
        return f"{self.challenge.name} {self.date_recorded}"
