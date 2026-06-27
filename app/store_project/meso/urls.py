from django.urls import path

from .views import AthleteProfileView
from .views import ChangeReviewView
from .views import DeliverView
from .views import MesoDesignerView
from .views import ResultsView
from .views import RosterView

app_name = "meso"
urlpatterns = [
    path("", RosterView.as_view(), name="roster"),
    path("designer/", MesoDesignerView.as_view(), name="designer"),
    path("review/", ChangeReviewView.as_view(), name="review"),
    path("deliver/", DeliverView.as_view(), name="deliver"),
    path("results/", ResultsView.as_view(), name="results"),
    path("athlete/<slug:slug>/", AthleteProfileView.as_view(), name="athlete"),
]
