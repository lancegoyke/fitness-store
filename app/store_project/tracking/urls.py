from django.urls import path

from store_project.tracking import views


app_name = "tracking"
urlpatterns = [
    path("", views.test_list, name="test_list"),
    path("<int:pk>/", views.test_detail, name="test_detail"),
    path(
        "<int:pk>/results/add/",
        views.test_result_create,
        name="test_result_create"
    ),
    path("<int:pk>/results/bulk/",
        views.test_result_bulk,
        name="test_result_bulk",
    ),
]
