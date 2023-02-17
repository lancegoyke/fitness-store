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
    path("meal-plan-builder/", views.MealPlanBuilderView.as_view(), name="meal_plan_builder"),
    path("meal/", views.MealListView.as_view(), name="meal_list"),
    path("meal/create/", views.MealCreateView.as_view(), name="meal_create"),
    path("meal/detail/<int:pk>/", views.MealDetailView.as_view(), name="meal_detail"),
    path("meal/update/<int:pk>/", views.MealUpdateView.as_view(), name="meal_update"),
    path("meal/delete/<int:pk>/", views.MealDeleteView.as_view(), name="meal_delete"),
    path("meal/meta-form/", views.meal_meta_update_form, name="meal_meta_update_form"),
    path("meal/meta-session/", views.meal_meta_session, name="meal_meta_session"),
    path("ingredient/", views.IngredientListView.as_view(), name="ingredient_list"),
    path("ingredient/nutrition-form/", views.ingredient_amount_form, name="ingredient_amount_form"),
    path("ingredient/nutrition-lookup/", views.ingredient_nutrition_lookup, name="ingredient_nutrition_lookup"),
    path("ingredient/search-local/", views.ingredient_search_local, name="ingredient_search_local"),
    path("ingredient/search-remote/", views.ingredient_search_remote, name="ingredient_search_remote"),
    path("ingredient/search/modal/", views.IngredientListSearchView.as_view(), name="ingredient_search_modal"),
    # path("ingredient/search/", views.IngredientListSearchView.as_view(), name="ingredient_search"),
    path("ingredient/create/", views.IngredientCreateView.as_view(), name="ingredient_create"),
    path("ingredient/detail/<int:pk>/", views.IngredientDetailView.as_view(), name="ingredient_detail"),
    path("ingredient/update/<int:pk>/", views.IngredientUpdateView.as_view(), name="ingredient_update"),
    path("ingredient/delete/<int:pk>/", views.IngredientDeleteView.as_view(), name="ingredient_delete"),
]
