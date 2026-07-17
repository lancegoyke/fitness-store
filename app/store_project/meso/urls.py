from django.urls import path

from . import views
from .views import AthleteHomeView
from .views import AthleteProfileView
from .views import AthleteSessionView
from .views import BecomeCoachView
from .views import ChangeReviewView
from .views import CoachBillingView
from .views import DeliverView
from .views import MesoDesignerView
from .views import OfflineView
from .views import RelationshipHistoryView
from .views import ResultsView
from .views import RosterView
from .views import UsageDashboardView

app_name = "meso"
urlpatterns = [
    path("", RosterView.as_view(), name="roster"),
    # Past athletes — ended/declined relationships, with re-invite.
    path(
        "history/",
        RelationshipHistoryView.as_view(),
        name="relationship_history",
    ),
    path("designer/", MesoDesignerView.as_view(), name="designer"),
    # Owner-facing agent usage + margin dashboard (agent-usage Phase 4) —
    # staff-gated, all-coach; the web read-out of meso_agent_usage_report.
    path("usage/", UsageDashboardView.as_view(), name="usage_dashboard"),
    # Owner-facing guided-tour funnel dashboard (#441 P3-6) — staff-gated, all-coach.
    path("tour/funnel/", views.TourFunnelView.as_view(), name="tour_funnel"),
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
    # Login-free, tokened opt-out from training-delivery emails (the email's
    # List-Unsubscribe link). The signed token authorizes; no login required.
    path(
        "unsubscribe/<str:token>/",
        views.unsubscribe_delivery_email,
        name="unsubscribe_delivery_email",
    ),
    # Self-coaching (guided-tour Phase 0): put the coach on their own roster
    # ("self" isn't a UUID, so this never shadows ``athlete/<uuid:pk>/``).
    path("athlete/self/add/", views.roster_add_self, name="roster_add_self"),
    path("athlete/<uuid:pk>/", AthleteProfileView.as_view(), name="athlete"),
    # Create (or open) an individual program for an athlete (first-time-UX
    # Phase 1) — the "+ New program" / "Build a program" CTAs.
    path("athlete/<uuid:pk>/plan/new/", views.plan_create, name="plan_create"),
    # Public, no-signup ephemeral sandbox (issue #389, Phase 1): the entry point
    # that mints/logs in a throwaway coach.
    path("demo/", views.sandbox_enter, name="sandbox_enter"),
    # One-click coach demo (first-time-UX Phase 2, Q3): load / remove a populated,
    # coach-scoped demo workspace.
    path("demo/load/", views.demo_load, name="demo_load"),
    path("demo/clear/", views.demo_clear, name="demo_clear"),
    # The sandbox's conversion hop: log a sandbox coach out, then hand off to
    # allauth signup (issue #389, Phase 1).
    path("demo/signup/", views.sandbox_signup, name="sandbox_signup"),
    # Guided demo onboarding tour (issue #430, Phase 2): advance/back/goto/
    # dismiss/complete/restart the step index, and the "skip · load
    # everything" shortcut (O6).
    path("tour/state/", views.tour_state, name="tour_state"),
    path("tour/skip/", views.tour_skip, name="tour_skip"),
    # Read-only snapshot of the coach's authoritative tour config (issue #451):
    # the mounted driver re-reads it after a self-variant deliver/results fetch
    # action to mirror the server-side auto-advance without a page reload.
    path("tour/config/", views.tour_config, name="tour_config"),
    # Email invites / onboarding (N4): coach sends/revokes, athlete claims.
    path("invite/", views.coach_invite, name="coach_invite"),
    path(
        "invite/<uuid:token>/revoke/",
        views.coach_invite_revoke,
        name="coach_invite_revoke",
    ),
    path(
        "invite/<uuid:token>/resend/",
        views.coach_invite_resend,
        name="coach_invite_resend",
    ),
    path("claim/<uuid:token>/", views.invite_claim, name="invite_claim"),
    # Peer-invite token actions on an existing CoachAthlete (Phase 1 spine).
    path("invite/<uuid:token>/accept/", views.invite_accept, name="invite_accept"),
    path("invite/<uuid:token>/decline/", views.invite_decline, name="invite_decline"),
    path(
        "relationship/<uuid:token>/end/",
        views.relationship_end,
        name="relationship_end",
    ),
    path(
        "relationship/<uuid:token>/reinvite/",
        views.relationship_reinvite,
        name="relationship_reinvite",
    ),
    # Athlete → coach requests (N4 Phase 2): the athlete asks, then may withdraw.
    path("request/", views.athlete_request_coach, name="athlete_request_coach"),
    path(
        "request/<uuid:token>/withdraw/",
        views.request_withdraw,
        name="request_withdraw",
    ),
    # Designer autosave API (Phase 3).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/",
        views.prescription_patch,
        name="api_prescription_patch",
    ),
    # Soft-delete an exercise row (designer framework Phase 0, issue #401).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/delete/",
        views.prescription_delete,
        name="api_prescription_delete",
    ),
    path(
        "api/plan/<int:plan_id>/session/<int:pk>/exercise/",
        views.session_add_exercise,
        name="api_session_add_exercise",
    ),
    # Add a training day to the plan's current week (first-time-UX Phase 1).
    path(
        "api/plan/<int:plan_id>/session/",
        views.session_add,
        name="api_session_add",
    ),
    # Soft-delete a training day (designer framework Phase 0, issue #401).
    path(
        "api/plan/<int:plan_id>/session/<int:pk>/delete/",
        views.session_delete,
        name="api_session_delete",
    ),
    # Reorder a session's exercise rows (dnd-kit designer, Phase 4, #403).
    path(
        "api/plan/<int:plan_id>/session/<int:pk>/reorder/",
        views.session_reorder,
        name="api_session_reorder",
    ),
    # Multi-week designer: view any week (read), add the next week (write), and
    # set the live/deliver-target week. ``week/<id>/`` GET views; POST sets current.
    path(
        "api/plan/<int:plan_id>/week/",
        views.week_add,
        name="api_week_add",
    ),
    path(
        "api/plan/<int:plan_id>/week/<int:week_id>/",
        views.week_view,
        name="api_week_view",
    ),
    # P1 multi-week table (backend): the current (or ``?mesocycle=``) block's
    # dense day × row × week grid — read-only, mirrors ``week_view``.
    path(
        "api/plan/<int:plan_id>/grid/",
        views.api_mesocycle_grid,
        name="api_mesocycle_grid",
    ),
    path(
        "api/plan/<int:plan_id>/week/<int:week_id>/current/",
        views.week_set_current,
        name="api_week_set_current",
    ),
    # Soft-delete a week (designer framework Phase 0, issue #401).
    path(
        "api/plan/<int:plan_id>/week/<int:week_id>/delete/",
        views.week_delete,
        name="api_week_delete",
    ),
    # Reorder a week's training days (dnd-kit designer, Phase 4, #403).
    path(
        "api/plan/<int:plan_id>/week/<int:week_id>/reorder/",
        views.week_reorder_sessions,
        name="api_week_reorder_sessions",
    ),
    # Plan-wide undo/redo op-log (designer framework Phase 1, issue #401).
    path(
        "api/plan/<int:plan_id>/undo/",
        views.api_plan_undo,
        name="api_plan_undo",
    ),
    path(
        "api/plan/<int:plan_id>/redo/",
        views.api_plan_redo,
        name="api_plan_redo",
    ),
    # Coach sets/clears an athlete's 1RM from the designer (1RM follow-up Phase 3).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/one-rm/",
        views.coach_set_one_rm,
        name="api_coach_set_one_rm",
    ),
    # Move an exercise row to a different session, within the same week
    # (dnd-kit designer cross-day drag, Phase 4, #403).
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/move/",
        views.prescription_move,
        name="api_prescription_move",
    ),
    # Freeform cell writes (Phase 2a): a sub-line upsert addressed by
    # (exercise_slot, week, line) — the stack is sparse, so no pk — and the
    # per-exercise Tempo/Rest/instructions columns on the row itself (D2).
    path(
        "api/plan/<int:plan_id>/row/<int:slot_id>/cell/",
        views.cell_line_write,
        name="api_cell_line_write",
    ),
    path(
        "api/plan/<int:plan_id>/row/<int:slot_id>/",
        views.exercise_slot_patch,
        name="api_exercise_slot_patch",
    ),
    # One-week exceptions: skip / fill-across-weeks (P2, issue #440). The swap
    # endpoint is retired (Phase 2a): a substitution is sub-line text now.
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/skip/",
        views.prescription_skip,
        name="api_prescription_skip",
    ),
    path(
        "api/plan/<int:plan_id>/prescription/<int:pk>/fill/",
        views.prescription_fill,
        name="api_prescription_fill",
    ),
    path(
        "api/plan/<int:plan_id>/deliver/",
        views.plan_deliver,
        name="api_plan_deliver",
    ),
    # Batch-deliver (2c): form POST fanning an independent COPY of the plan
    # out to the selected clients — the group subsystem's replacement (D1).
    path(
        "plan/<int:plan_id>/batch-deliver/",
        views.plan_batch_deliver,
        name="plan_batch_deliver",
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
    # Self-serve coach signup (S6 Phase 4): the public become-a-coach funnel —
    # a landing page + the action that creates the CoachProfile.
    path("coach/", BecomeCoachView.as_view(), name="become_coach"),
    path("coach/start/", views.start_coaching, name="start_coaching"),
    # Billing (S6): the coach-facing plan/usage page, then subscribe via Stripe
    # Checkout, manage via the hosted Portal, and the clean subscription webhook
    # (separate from the products webhook).
    path("billing/", CoachBillingView.as_view(), name="billing"),
    path("billing/subscribe/", views.billing_subscribe, name="billing_subscribe"),
    path("billing/portal/", views.billing_portal, name="billing_portal"),
    # Start the no-card local trial (S6 Phase 3) — the free path to full access.
    path("billing/trial/", views.billing_start_trial, name="billing_start_trial"),
    path("billing/webhook/", views.billing_webhook, name="billing_webhook"),
]
