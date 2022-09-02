from django.contrib.contenttypes.fields import ContentType
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext as _

from embed_video.fields import EmbedVideoField

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
    video = EmbedVideoField(blank=True, default=None)
    measurement_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        verbose_name=_("Type of measurement"),
        null=True,
        default=None,
        limit_choices_to=models.Q(model="loadmeasure")
        | models.Q(model="powermeasure")
        | models.Q(model="distancemeasure")
        | models.Q(model="durationmeasure"),
    )
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)
    author = models.ForeignKey(
        User,
        verbose_name=_("Person who added the test"),
        null=True,
        on_delete=models.SET_NULL,
        limit_choices_to={"is_staff": True},
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_DEFAULT,
        null=True,
        default=None,
    )

    def __str__(self):
        return f"{self.name}"

    def get_absolute_url(self):
        return reverse("tracking:test_detail", kwargs={"pk": self.pk})

    def get_measure_base_form_cls(self):
        return self.measurement_type.model_class()().get_form()

    def get_measure_staff_form_cls(self):
        return self.measurement_type.model_class()().get_staff_form()

    def get_measure_athlete_form_cls(self):
        return self.measurement_type.model_class()().get_athlete_form()


class AbstractMeasure(models.Model):
    """
    A point of performance occuring at a particular time and body
    """

    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    value = models.PositiveIntegerField(help_text="Whole number")
    unit = models.CharField(max_length=3)
    created = models.DateTimeField(_("Time created"), auto_now_add=True)
    modified = models.DateTimeField(_("Time last modified"), auto_now=True)

    class Meta:
        abstract = True

    def get_base_form(self):
        raise NotImplementedError(
            "Make sure the subclass implements its own MeasureBaseForm"
        )

    def get_staff_form(self):
        raise NotImplementedError(
            "Make sure the subclass implements its own MeasureStaffForm"
        )

    def get_athlete_form(self):
        raise NotImplementedError(
            "Make sure the subclass implements its own MeasureTestForm"
        )

    def __str__(self):
        return f"{self.test} for {self.user} - {self.value} {self.unit}"


class UnitsOfLoad(models.TextChoices):
    POUNDS = "lb", _("Pounds")
    KILOS = "kg", _("Kilograms")


class LoadMeasure(AbstractMeasure):
    unit = models.CharField(max_length=2, choices=UnitsOfLoad.choices)

    def get_base_form(self):
        from .forms import LoadMeasureBaseForm

        return LoadMeasureBaseForm

    def get_staff_form(self):
        from .forms import LoadMeasureStaffForm

        return LoadMeasureStaffForm

    def get_athlete_form(self):
        from .forms import LoadMeasureTestForm

        return LoadMeasureTestForm


class UnitsOfPower(models.TextChoices):
    WATTS = "W", _("Watts")


class PowerMeasure(AbstractMeasure):
    unit = models.CharField(max_length=2, choices=UnitsOfPower.choices)

    def get_base_form(self):
        from .forms import PowerMeasureBaseForm

        return PowerMeasureBaseForm

    def get_staff_form(self):
        from .forms import PowerMeasureStaffForm

        return PowerMeasureStaffForm

    def get_athlete_form(self):
        from .forms import PowerMeasureTestForm

        return PowerMeasureTestForm


class UnitsOfTime(models.TextChoices):
    DURATION = "d", _("Duration")
    SECONDS = "s", _("Seconds")
    MICROSECONDS = "μs", _("Microseconds")


class DurationMeasure(AbstractMeasure):
    """A period of time"""

    value = models.DurationField()
    unit = models.CharField(
        max_length=10,
        choices=UnitsOfTime.choices,
        default=UnitsOfTime.DURATION,
    )

    def get_base_form(self):
        from .forms import DurationMeasureBaseForm

        return DurationMeasureBaseForm

    def get_staff_form(self):
        from .forms import DurationMeasureStaffForm

        return DurationMeasureStaffForm

    def get_athlete_form(self):
        from .forms import DurationMeasureTestForm

        return DurationMeasureTestForm


class UnitsOfDistance(models.TextChoices):
    MILES = "mi", _("Miles")
    METERS = "m", _("Meters")
    FEET = "ft", _("Feet")
    INCHES = "in", _("Inches")
    YARDS = "yd", _("Yards")


class DistanceMeasure(AbstractMeasure):
    """Distance traveled"""

    unit = models.CharField(max_length=2, choices=UnitsOfDistance.choices)

    def get_base_form(self):
        from .forms import DistanceMeasureBaseForm

        return DistanceMeasureBaseForm

    def get_staff_form(self):
        from .forms import DistanceMeasureStaffForm

        return DistanceMeasureStaffForm

    def get_athlete_form(self):
        from .forms import DistanceMeasureTestForm

        return DistanceMeasureTestForm
