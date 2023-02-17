from django.core.validators import validate_comma_separated_integer_list
from django.db import models
from django.urls import reverse


class Unit(models.Model):

    # Fields
    id = models.UUIDField(primary_key=True)
    description = models.CharField(max_length=100)
    abbr = models.CharField(max_length=10)
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_updated = models.DateTimeField(auto_now=True, editable=False)

    def __str__(self):
        return self.description


class Nutrient(models.Model):

    # Fields
    id = models.UUIDField(primary_key=True)
    description = models.CharField(max_length=100)
    unit = models.CharField(max_length=10, null=True)
    unit_id = models.UUIDField(null=True)
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_updated = models.DateTimeField(auto_now=True, editable=False)

    def __str__(self):
        return f"{self.description}"


class Ingredient(models.Model):

    # Fields
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_updated = models.DateTimeField(auto_now=True, editable=False)
    spoon_id = models.CharField(max_length=25)  # from API
    name = models.CharField(max_length=100)
    amount = models.PositiveSmallIntegerField()
    unit = models.CharField(max_length=30)
    cals = models.PositiveSmallIntegerField()
    net_cals = models.PositiveSmallIntegerField()
    fat = models.PositiveSmallIntegerField()
    carbs = models.PositiveSmallIntegerField()
    fiber = models.PositiveSmallIntegerField()
    net_carbs = models.PositiveSmallIntegerField()
    protein = models.PositiveSmallIntegerField()

    class Meta:
        pass

    def __str__(self):
        return f"{self.amount} {self.unit} of {self.name}"

    def get_absolute_url(self):
        return reverse("meals:ingredient_detail", args=(self.pk,))

    def get_update_url(self):
        return reverse("meals:ingredient_update", args=(self.pk,))


class Meal(models.Model):

    # Fields
    name = models.CharField(max_length=100, blank=True, default=None)
    description = models.TextField(max_length=2000, blank=True, default=None)
    created = models.DateTimeField(auto_now_add=True, editable=False)
    last_updated = models.DateTimeField(auto_now=True, editable=False)
    ingredients = models.CharField(validators=[validate_comma_separated_integer_list], max_length=200)
    cals = models.PositiveSmallIntegerField(blank=True, default=None)
    net_cals = models.PositiveSmallIntegerField(blank=True, default=None)
    fat = models.PositiveSmallIntegerField(blank=True, default=None)
    carbs = models.PositiveSmallIntegerField(blank=True, default=None)
    fiber = models.PositiveSmallIntegerField(blank=True, default=None)
    net_carbs = models.PositiveSmallIntegerField(blank=True, default=None)
    protein = models.PositiveSmallIntegerField(blank=True, default=None)

    class Meta:
        pass

    def __str__(self):
        return str(self.pk)

    def get_absolute_url(self):
        return reverse("meals:meal_detail", args=(self.pk,))

    def get_update_url(self):
        return reverse("meals:meal_update", args=(self.pk,))
