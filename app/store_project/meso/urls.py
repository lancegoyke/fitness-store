from django.urls import path

from .views import MesoDesignerView

app_name = "meso"
urlpatterns = [
    path("", MesoDesignerView.as_view(), name="designer"),
]
