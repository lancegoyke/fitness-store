from django.urls import path

from .views import cardio_create

app_name = "cardio"
urlpatterns = [
    path("", cardio_create, name="create"),
]
