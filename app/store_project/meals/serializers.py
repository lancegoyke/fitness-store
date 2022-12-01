from rest_framework import serializers

from store_project.meals import models


class MealSerializer(serializers.ModelSerializer):

    class Meta:
        model = models.Meal
        fields = [
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

class IngredientSerializer(serializers.ModelSerializer):

    class Meta:
        model = models.Ingredient
        fields = [
            "spoon_id",
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
