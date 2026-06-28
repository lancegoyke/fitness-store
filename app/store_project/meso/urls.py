from django.urls import path

from . import views
from .views import AthleteHomeView
from .views import AthleteProfileView
from .views import AthleteSessionView
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
    path(
        "review/<int:batch_id>/",
        ChangeReviewView.as_view(),
        name="review_batch",
    ),
    path("deliver/", DeliverView.as_view(), name="deliver"),
    path("deliver/<int:plan_id>/", DeliverView.as_view(), name="deliver_plan"),
    path("results/", ResultsView.as_view(), name="results"),
    # Athlete surface (athlete slice Phase 1) — the athlete's own training view,
    # distinct from the coach's ``athlete/<uuid>/`` record.
    path("me/", AthleteHomeView.as_view(), name="athlete_home"),
    path(
        "me/session/<int:pk>/",
        AthleteSessionView.as_view(),
        name="athlete_session",
    ),
    path(
        "api/me/session/<int:pk>/log/",
        views.athlete_log_session,
        name="athlete_log_session",
    ),
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
    # Agent proposal engine (agent slice Phase 1; async + status poll Phase 4).
    path(
        "api/plan/<int:plan_id>/agent/",
        views.agent_propose,
        name="api_plan_agent",
    ),
    path(
        "api/batch/<int:batch_id>/status/",
        views.batch_status,
        name="api_batch_status",
    ),
    # Review gate: approve/reject + apply (agent slice Phase 2).
    path(
        "api/change/<int:pk>/status/",
        views.change_set_status,
        name="api_change_status",
    ),
    path(
        "api/batch/<int:batch_id>/apply/",
        views.batch_apply,
        name="api_batch_apply",
    ),
    path(
        "api/batch/<int:batch_id>/dismiss/",
        views.batch_dismiss,
        name="api_batch_dismiss",
    ),
]
