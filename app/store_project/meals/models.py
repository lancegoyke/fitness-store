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
    name = models.CharField(max_length=100)
    amount = models.IntegerField()
    unit = models.CharField(max_length=30)
    fiber = models.PositiveSmallIntegerField()
    cals = models.PositiveSmallIntegerField()
    fat = models.PositiveSmallIntegerField()
    carbs = models.PositiveSmallIntegerField()
    net_carbs = models.PositiveSmallIntegerField()
    net_cals = models.PositiveSmallIntegerField()
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
    fat = models.PositiveSmallIntegerField()
    created = models.DateTimeField(auto_now_add=True, editable=False)
    ingredients = models.CharField(validators=[validate_comma_separated_integer_list], max_length=200)
    cals = models.PositiveSmallIntegerField()
    fiber = models.PositiveSmallIntegerField()
    protein = models.PositiveSmallIntegerField()
    net_carbs = models.PositiveSmallIntegerField()
    last_updated = models.DateTimeField(auto_now=True, editable=False)
    description = models.TextField(max_length=2000)
    net_cals = models.PositiveSmallIntegerField()
    carbs = models.PositiveSmallIntegerField()

    class Meta:
        pass

    def __str__(self):
        return str(self.pk)

    def get_absolute_url(self):
        return reverse("meals:meal_detail", args=(self.pk,))

    def get_update_url(self):
        return reverse("meals:meal_update", args=(self.pk,))
