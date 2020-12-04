from django.urls import path

from store_project.exercises.views import ExerciseDetailView, ExerciseListView

app_name = "exercises"
urlpatterns = [
    path("", ExerciseListView.as_view(), name="list"),
    path("<str:slug>/", ExerciseDetailView.as_view(), name="detail"),
]
