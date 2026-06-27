from django.urls import path

from . import views
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
    path("designer/<int:plan_id>/", MesoDesignerView.as_view(), name="designer_plan"),
    path("review/", ChangeReviewView.as_view(), name="review"),
    path("deliver/", DeliverView.as_view(), name="deliver"),
    path("deliver/<int:plan_id>/", DeliverView.as_view(), name="deliver_plan"),
    path("results/", ResultsView.as_view(), name="results"),
    path("athlete/<uuid:pk>/", AthleteProfileView.as_view(), name="athlete"),
    path("invite/<uuid:token>/accept/", views.invite_accept, name="invite_accept"),
    path("invite/<uuid:token>/decline/", views.invite_decline, name="invite_decline"),
    path(
        "relationship/<uuid:token>/end/",
        views.relationship_end,
        name="relationship_end",
    ),
    # Designer autosave API (Phase 3).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/",
        views.prescription_patch,
        name="api_prescription_patch",
    ),
    path(
        "api/plan/<int:plan_id>/session/<int:pk>/exercise/",
        views.session_add_exercise,
        name="api_session_add_exercise",
    ),
    path(
        "api/plan/<int:plan_id>/deliver/",
        views.plan_deliver,
        name="api_plan_deliver",
    ),
]
