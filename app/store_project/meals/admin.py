from django.contrib import admin
from django import forms

from store_project.meals import models


class MealAdminForm(forms.ModelForm):

    class Meta:
        model = models.Meal
        fields = "__all__"


class MealAdmin(admin.ModelAdmin):
    form = MealAdminForm
    list_display = [
        "ingredients",
        "net_cals",
        "description",
        "fat",
        "cals",
        "created",
        "last_updated",
        "carbs",
        "net_carbs",
        "fiber",
        "protein",
    ]
    readonly_fields = [
        "ingredients",
        "net_cals",
        "description",
        "fat",
        "cals",
        "created",
        "last_updated",
        "carbs",
        "net_carbs",
        "fiber",
        "protein",
    ]


class IngredientAdminForm(forms.ModelForm):

    class Meta:
        model = models.Ingredient
        fields = "__all__"


class IngredientAdmin(admin.ModelAdmin):
    form = IngredientAdminForm
    list_display = [
        "fiber",
        "carbs",
        "amount",
        "created",
        "fat",
        "last_updated",
        "name",
        "net_carbs",
        "cals",
        "unit",
        "net_cals",
        "protein",
    ]
    readonly_fields = [
        "fiber",
        "carbs",
        "amount",
        "created",
        "fat",
        "last_updated",
        "name",
        "net_carbs",
        "cals",
        "unit",
        "net_cals",
        "protein",
    ]


admin.site.register(models.Meal, MealAdmin)
admin.site.register(models.Ingredient, IngredientAdmin)
