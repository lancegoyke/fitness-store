from django.urls import path

from store_project.meals import views


app_name = "meals"
urlpatterns = [
    path("macro-calculator/", views.macro_calculator, name="macro_calculator"),
]
