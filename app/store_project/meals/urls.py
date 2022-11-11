from django.urls import path, include
from rest_framework import routers

from store_project.meals import api
from store_project.meals import views


router = routers.DefaultRouter()
router.register("meal", api.MealViewSet)
router.register("ingredient", api.IngredientViewSet)

app_name = "meals"
urlpatterns = [
    path("api/v1/", include(router.urls)),
    path("macro-calculator/", views.macro_calculator, name="macro_calculator"),
    path("meal/", views.MealListView.as_view(), name="meal_list"),
    path("meal/create/", views.MealCreateView.as_view(), name="meal_create"),
    path("meal/detail/<int:pk>/", views.MealDetailView.as_view(), name="meal_detail"),
    path("meal/update/<int:pk>/", views.MealUpdateView.as_view(), name="meal_update"),
    path("meal/delete/<int:pk>/", views.MealDeleteView.as_view(), name="meal_delete"),
    path("ingredient/", views.IngredientListView.as_view(), name="ingredient_list"),
    path("ingredient/create/", views.IngredientCreateView.as_view(), name="ingredient_create"),
    path("ingredient/detail/<int:pk>/", views.IngredientDetailView.as_view(), name="ingredient_detail"),
    path("ingredient/update/<int:pk>/", views.IngredientUpdateView.as_view(), name="ingredient_update"),
    path("ingredient/delete/<int:pk>/", views.IngredientDeleteView.as_view(), name="ingredient_delete"),
]
