from django.urls import path
from django.views.generic.base import TemplateView

from .views import cardio_create

app_name = "cardio"
urlpatterns = [
    path("", cardio_create, name="create"),
]
