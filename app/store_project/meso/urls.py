from django.urls import path

from . import views
from .views import AthleteHomeView
from .views import AthleteProfileView
from .views import AthleteSessionView
from .views import ChangeReviewView
from .views import DeliverView
from .views import GroupDetailView
from .views import MesoDesignerView
from .views import OfflineView
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
    path(
        "results/<int:session_id>/",
        ResultsView.as_view(),
        name="results_session",
    ),
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
    # Set/clear the athlete's manual 1RM for a lift (Phase 2 — server-persisted).
    path(
        "api/me/session/<int:pk>/one-rm/",
        views.athlete_set_one_rm,
        name="athlete_set_one_rm",
    ),
    # Athlete PWA (Phase 4b — S7): manifest + service worker + offline shell.
    # Served as views (not static files) so the worker has a stable /meso/-scoped
    # URL the hashing static pipeline can't give it.
    path("manifest.webmanifest", views.manifest_webmanifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", OfflineView.as_view(), name="offline"),
    # Web push subscribe / unsubscribe (Phase 4b — S3/S7).
    path("api/me/push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path(
        "api/me/push/unsubscribe/",
        views.push_unsubscribe,
        name="push_unsubscribe",
    ),
    path("athlete/<uuid:pk>/", AthleteProfileView.as_view(), name="athlete"),
    path("group/new/", views.group_create, name="group_create"),
    path("group/<int:pk>/", GroupDetailView.as_view(), name="group"),
    path("group/<int:pk>/design/", views.group_design, name="group_design"),
    path("group/<int:pk>/deliver/", views.group_deliver, name="group_deliver"),
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
    # Per-athlete override on a group's shared program (groups Phase 3).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/override/",
        views.prescription_override,
        name="api_prescription_override",
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
