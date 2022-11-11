from rest_framework import viewsets, permissions

from store_project.meals import serializers
from store_project.meals import models


class MealViewSet(viewsets.ModelViewSet):
    """ViewSet for the Meal class"""

    queryset = models.Meal.objects.all()
    serializer_class = serializers.MealSerializer
    permission_classes = [permissions.IsAuthenticated]


class IngredientViewSet(viewsets.ModelViewSet):
    """ViewSet for the Ingredient class"""

    queryset = models.Ingredient.objects.all()
    serializer_class = serializers.IngredientSerializer
    permission_classes = [permissions.IsAuthenticated]
