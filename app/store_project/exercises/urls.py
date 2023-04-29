from django.urls import path

from store_project.exercises.views import (
    ExerciseDetailView,
    ExerciseFilteredListView,
    ExerciseListView,
    search,
)

app_name = "exercises"
urlpatterns = [
    path("", ExerciseListView.as_view(), name="list"),
    path(
        "category/<str:category>/",
        ExerciseFilteredListView.as_view(),
        name="filtered_list",
    ),
    path("search/", search, name="search"),
    path("<str:slug>/", ExerciseDetailView.as_view(), name="detail"),
]
