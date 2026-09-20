import datetime
import ipaddress
import json
import logging
from urllib.parse import urlencode
from urllib.parse import urlparse

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import login
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection
from django.db import transaction
from django.db.models import Count
from django.db.models import Max
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import HttpResponseNotFound
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from store_project.analytics.events import EventName
from store_project.analytics.track import track
from store_project.notifications.emails import send_block_delivered_email
from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.push import record_push_click

from . import adherence as meso_adherence
from . import demo as meso_demo
from . import one_rm as meso_one_rm
from . import parsing
from . import presenters
from . import push as meso_push
from . import sandbox as meso_sandbox
from . import tour as meso_tour
from .agent import apply as agent_apply
from .agent import client as agent_client
from .agent import jobs as agent_jobs
from .agent import service as agent_service
from .billing import access as billing_access
from .billing import agent_usage_report as usage_report
from .billing import stripe_gateway as billing_gateway
from .billing import webhooks as billing_webhooks
from .history import HistoryUnavailable
from .history import record_plan_action
from .history import restore_plan_snapshot
from .history import serialize_plan_snapshot
from .models import MAX_LOGGED_SET_NUMBER
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachProfile
from .models import CoachSubscription
from .models import ExerciseSlot
from .models import InvalidTransition
from .models import LoggedSet
from .models import Mesocycle
from .models import Plan
from .models import PlanAction
from .models import Prescription
from .models import ProposedChange
from .models import PushSubscription
from .models import SandboxSession
from .models import Session
from .models import SessionLog
from .models import SessionSlot
from .models import Week
from .models import WeekDelivery
from .models import display_line_id
from .models import hidden_parsed_set_pks
from .models import newest_session_logs
from .models import sub_line_warn_reason
from .parsing import parse_performed
from .parsing import performed_reps_text
from .personal_records import new_records_in
from .serializers import current_week
from .serializers import first_live_week
from .serializers import serialize_chat_thread
from .serializers import serialize_mesocycle_grid
from .serializers import serialize_new_record
from .serializers import serialize_plan
from .serializers import serialize_plan_history
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change
from .serializers import serialize_session
from .serializers import serialize_session_log
from .serializers import serialize_week_snapshot
from .unsubscribe import athlete_opted_out
from .unsubscribe import make_unsubscribe_token
from .unsubscribe import resolve_unsubscribe_user
from .unsubscribe import set_delivery_email_opt_out

logger = logging.getLogger(__name__)

User = get_user_model()


def _is_coach(user):
    """Whether ``user`` is acting as a coach (the roster / billing surfaces' gate).

    A user counts as a coach if they have a ``CoachProfile`` *or* any coach-side
    link (athletes they coach, including a pending request awaiting them) *or* a
    sent email invite — anyone else is a pure athlete.
    """
    return (
        CoachProfile.objects.filter(user=user).exists()
        or CoachAthlete.objects.for_coach(user).exists()
        or CoachInvite.objects.for_coach(user).exists()
    )


# -- billing gates (S6 Phase 3) -------------------------------------------
#
# The paywall gets teeth here. ``billing/access.py`` owns the predicates; these
# shape the rejection per surface — a flashed redirect for the form views, a 402
# JSON body for the autosave/deliver API. Three gates: the seat cap blocks a free
# coach past the limit at the relationship choke points (``can_add_athlete``); the
# AI agent is paid-only (``can_use_agent``); and an over-limit coach (post-downgrade,
# D6) is frozen out of edits/deliver (``can_edit``).

#: Flashed when a free coach hits the seat cap — the upgrade CTA the roster shows.
SEAT_LIMIT_MESSAGE = (
    "You've reached your free athlete limit. Start your free trial or subscribe "
    "to add more athletes."
)

#: Flashed on a form view when an over-limit coach (D6) tries to edit/deliver.
OVER_LIMIT_MESSAGE = (
    "You're over your plan's athlete limit. Re-subscribe or end a relationship "
    "to edit or deliver programs."
)

#: Flashed when a free coach asks the AI to draft a plan but is out of monthly runs.
DRAFT_ALLOWANCE_MESSAGE = (
    "You're out of free AI agent runs this month, so your program starts blank. "
    "Start your free trial or subscribe for unlimited agent runs."
)


def _over_limit_json():
    """402 JSON for an API edit/deliver blocked by the D6 over-limit freeze."""
    return JsonResponse(
        {
            "ok": False,
            "error": (
                "You're over your plan's athlete limit. Re-subscribe or end a "
                "relationship to keep editing."
            ),
            "over_limit": True,
        },
        status=402,
    )


def _coach_working_plan(user, *, plans=None):
    """The coach's most-recently-touched, non-archived plan, or None.

    The target a bare ``/meso/designer/`` or ``/meso/deliver/`` URL resolves to:
    the plan the coach last worked, or back on the roster if they have none.
    ``plans`` overrides the candidate set; the default is the coach's editable
    plans (``for_coach``).
    """
    qs = plans if plans is not None else Plan.objects.for_coach(user)
    return qs.exclude(status=Plan.Status.ARCHIVED).order_by("-modified").first()


def _coach_session_or_404(user, pk):
    """A session on a plan the coach owns (active relationship), or ``Http404``.

    The coach-side analogue of ``_athlete_session_or_404``: a foreign athlete's
    session or an unknown id are an indistinguishable flat 404 (no leak). Used by
    the results screen; delivery isn't required — a logged session is logged.
    """
    session = (
        Session.objects.filter(
            pk=pk, week__mesocycle__plan__in=Plan.objects.for_coach(user)
        )
        .select_related("week__mesocycle__plan__relationship")
        .first()
    )
    if session is None:
        raise Http404("Unknown session")
    return session


def _coach_latest_logged_session(user):
    """The coach's most-recently *completed* session across their athletes, or None.

    The target the bare ``/meso/results/`` resolves to. Only *done* logs count —
    a pending draft isn't a result yet (the results screen would render it as an
    awaiting session anyway). Ordered by the workout date (then created) so the
    coach lands on the session most recently trained.
    """
    log = (
        SessionLog.objects.filter(
            session__week__mesocycle__plan__in=Plan.objects.for_coach(user),
            status=SessionLog.Status.DONE,
        )
        .select_related("session")
        .order_by("-date", "-created_at")
        .first()
    )
    return log.session if log else None


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso strength-training program designer.

    A self-contained, full-screen coach tool. The view serializes a real, owned
    plan into the page and the Alpine front-end hydrates from it (then autosaves
    edits to the API endpoints below). The bare URL has no fixtures anymore — it
    redirects to the coach's working plan (or the roster). The agent column is
    live (agent slice) and its conversation is persisted: ``chat_thread``
    rebuilds the thread from the plan's proposal batches so it survives a reload.
    """

    template_name = "meso/designer.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("plan_id") is None:
            plan = _coach_working_plan(
                request.user, plans=Plan.objects.editable_by(request.user)
            )
            if plan is None:
                messages.info(request, "Pick an athlete to start a program.")
                return redirect("meso:roster")
            return redirect("meso:designer_plan", plan_id=plan.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        plan = (
            Plan.objects.editable_by(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        # P1 multi-week table (backend): the current block's dense day × row ×
        # week grid — issue #455 phase A5 made ``useGrid``/``MesoTable`` the
        # island's sole data owner, so this is now the only hydration payload
        # the designer needs (``serialize_plan`` itself survives unchanged —
        # it's still load-bearing for the agent's ``build_context``, see
        # serializers.py). Uses the same block resolution ``serialize_plan``
        # used to (``current_week``'s default mesocycle); left unset for a plan with
        # no block at all (shouldn't happen post-scaffold, but a corrupt/
        # legacy row shouldn't 500 the whole designer — it renders a blank
        # island instead, per CONTRACT.md).
        mesocycle = _default_grid_mesocycle(plan)
        if mesocycle is not None:
            ctx["grid_data"] = serialize_mesocycle_grid(mesocycle)
        # The persisted agent conversation, rebuilt from this plan's proposal
        # batches so the chat survives a reload (the JS hydrates ``messages``
        # from it, falling back to the greeting when empty).
        ctx["chat_thread"] = serialize_chat_thread(plan)
        # Agent gate (S6 Phase 3, D4; Phase 5 metering): an active coach is
        # unlimited; a free coach gets a monthly allowance. The meter drives the
        # composer-vs-upgrade-CTA and the "N of M runs left" note; ``can_use_agent``
        # is derived from it so the page does one read (the endpoint also 402s, so
        # the gate is defended server-side, not just hidden).
        agent_meter = presenters.agent_allowance(self.request.user)
        ctx["agent_allowance"] = agent_meter
        ctx["can_use_agent"] = agent_meter["can_use"]
        ctx["price_summary"] = presenters.PRICE_SUMMARY
        # Designer island flags (Phase 2 PR B, frontend/designer/CONTRACT.md):
        # the React island replaces the template's server-side
        # {% if is_sandbox %}/{% elif can_use_agent %}/{% else %} composer gate
        # with this one json_script payload it branches on client-side. No new
        # predicate — ``is_sandbox`` is the same call the ``sandbox_status``
        # context processor makes (unavailable here: context processors only
        # apply at render time, after get_context_data), and the other three
        # values already exist above; this just also feeds the island.
        ctx["designer_flags"] = {
            "is_sandbox": meso_sandbox.is_sandbox(self.request.user),
            "can_use_agent": ctx["can_use_agent"],
            "agent_allowance": agent_meter,
            "signup_url": reverse("meso:sandbox_signup"),
            "price_summary": presenters.PRICE_SUMMARY,
        }
        ctx["phone_fallback"] = self._phone_fallback(plan, ctx.get("grid_data"))
        return ctx

    @staticmethod
    def _phone_fallback(plan, grid_data):
        """What designer.html shows instead of the island under 900px (#508).

        The designer isn't editable on a phone, so a coach who opens it there
        gets links to what they can use: the athlete's profile, and the deliver
        screen for the block on screen (the same ``?week=`` as the island's own
        Deliver link, and none when the block has no live week, as there).
        A template has no athlete and isn't delivered (``DeliverView`` bounces
        it back here), so it gets neither.
        """
        fallback = {"title": plan.title, "is_template": plan.is_template}
        if plan.is_template:
            return fallback
        athlete = plan.relationship.athlete
        fallback["athlete_name"] = athlete.display_name()
        fallback["athlete_url"] = reverse("meso:athlete", kwargs={"pk": athlete.pk})
        weeks = (grid_data or {}).get("weeks") or []
        if weeks:
            deliver_url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
            fallback["deliver_url"] = (
                f"{deliver_url}?{urlencode({'week': weeks[0]['id']})}"
            )
        return fallback


class RosterView(TemplateView):
    """The front door (``/meso/``) — splits on auth (first-time-UX Phase 3).

    - An **anonymous** visitor sees the public landing (what Meso is + two honest
      entry actions: log in as an athlete, or become a coach) rather than a bare
      login wall — Meso has to be legible before you have an account.
    - An **authenticated** visitor keeps the post-#311 role routing. The roster
      is a *coach* surface, so anyone not acting as a coach is sent to their
      training home — where they see their programs, respond to a coach's
      invite, and request a coach (N4 Phase 2). A user counts as a coach if they
      have a ``CoachProfile`` *or* any coach-side link (athletes they coach,
      including a pending request awaiting them) *or* a sent email invite.
      Everyone else — a pure athlete, an athlete awaiting an invite, or a
      brand-new user — lands on ``/meso/me/``.

    Not ``LoginRequiredMixin`` (which would bounce the anonymous visitor straight
    to login, the thing Phase 3 removes); the authenticated branches read
    ``request.user`` only after the anonymous one returns.
    """

    template_name = "meso/roster.html"

    def get(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return render(
                request,
                "meso/landing.html",
                {
                    "athlete_next": reverse("meso:athlete_home"),
                    # Names the trial on the coach card + demo card (issue #416)
                    # — same value ``become_coach`` exposes, so a future
                    # ``TRIAL_DAYS`` change can't leave the landing copy stale.
                    "trial_days": CoachSubscription.TRIAL_DAYS,
                    # The flat-price line on the coach card (issue #418) — the
                    # same constant the roster/billing/designer surfaces render,
                    # so the price can't drift out of sync with the landing page.
                    "price_summary": presenters.PRICE_SUMMARY,
                    # The hosted walkthrough video (issue #415 follow-up to
                    # #388) — hidden by default (issue #454; blank is the
                    # default in settings). Still settings-driven so setting
                    # a URL is the entire re-enable story: `just record-demo
                    # && just publish-demo-video`, then set
                    # MESO_DEMO_VIDEO_URL (template checks
                    # `{% if demo_video_url %}`).
                    "demo_video_url": settings.MESO_DEMO_VIDEO_URL,
                    "demo_video_poster_url": settings.MESO_DEMO_VIDEO_POSTER_URL,
                },
            )
        if not _is_coach(request.user):
            return redirect("meso:athlete_home")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        links = list(
            CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .select_related("athlete", "athlete__athlete_profile")
            .order_by("athlete__name", "athlete__email")
        )
        # The downgrade soft-suspends every active link beyond the oldest free cap
        # (S6 Phase 5); flag those rows so the roster shows a "Suspended" badge.
        suspended = billing_access.suspended_athlete_ids(self.request.user)
        # Relationships that already have an editable working plan (mirrors
        # ``working_plan``: non-archived). The roster hides the "Draft with AI"
        # CTA for these — ``plan_create`` reopens an existing plan rather than
        # drafting, so the action would be a no-op.
        have_plan = set(
            Plan.objects.filter(
                relationship_id__in=[link.pk for link in links],
            )
            .exclude(status=Plan.Status.ARCHIVED)
            .values_list("relationship_id", flat=True)
        )
        athletes = [
            presenters.roster_athlete(
                link.athlete,
                suspended=link.pk in suspended,
                demo=link.is_demo,
                self_link=link.is_self,
                has_working_plan=link.pk in have_plan,
                # Cadence signal (§4a, decided 2026-07-18): days since the
                # athlete's last done log, read-side over their own logs;
                # ``None`` renders as "No sessions yet".
                recency_days=meso_adherence.link_recency_days(link),
            )
            for link in links
        ]
        ctx["active"] = "roster"
        ctx["athletes"] = athletes
        # Outstanding email invites the coach has sent — pending *or* expired (N4);
        # an expired one still shows so the coach can Resend it (Phase 3).
        outstanding_invites = CoachInvite.objects.for_coach(
            self.request.user
        ).outstanding()
        ctx["pending_invites"] = [
            presenters.pending_invite(inv) for inv in outstanding_invites
        ]
        # Pending athlete→coach requests awaiting this coach's reply (N4 Phase 2).
        pending_requests = (
            CoachAthlete.objects.for_coach(self.request.user)
            .filter(status=CoachAthlete.Status.PENDING_ATHLETE_REQUEST)
            .select_related("athlete")
            .order_by("-created_at")
        )
        ctx["pending_requests"] = [
            presenters.pending_request(link) for link in pending_requests
        ]
        # Billing/paywall state (S6 Phase 3): tier, seat usage, and the upgrade
        # CTAs (start trial / subscribe / manage billing).
        ctx["billing"] = presenters.billing_state(
            self.request.user,
            checkout_pending=_checkout_pending(self.request),
        )
        # Recent-activity feed: the coach's athletes' latest completed sessions.
        ctx["activity"] = presenters.roster_activity(self.request.user)
        # Needs-review (agent batch state) is a separate slice — still neutral.
        ctx["needs_review"] = 0
        # First-run UX (Phase 2): a fresh coach with nothing yet gets an
        # onboarding card that teaches the model and offers the one-click demo;
        # once demo data is loaded a banner offers to remove it (Q3).
        ctx["has_demo"] = meso_demo.has_demo(self.request.user)
        ctx["is_empty"] = not athletes
        # Self-coaching (guided-tour Phase 0): the roster offers "Add yourself as
        # an athlete" until the coach's one self-link is active.
        ctx["has_self_link"] = any(link.is_self for link in links)
        # Guided-tour Phase 3: an empty workspace's Get-started card becomes the
        # tour entry point for anyone whose tour hasn't been dismissed/completed
        # (covers "never started" — the common real-coach case — and, harmlessly,
        # an in-progress tour, though that branch never renders since the tour
        # itself is mounted instead whenever ``show_meso_tour`` is true). Once
        # dismissed/completed, this reads False and the original card returns —
        # nothing is ever a dead end.
        ctx["tour_entry_available"] = meso_tour.is_active(self.request.user)
        return ctx


class RelationshipHistoryView(LoginRequiredMixin, TemplateView):
    """Past athletes (``/meso/history/``) — the coach surface for closed links.

    An ended or declined ``CoachAthlete`` vanishes from the active roster, but the
    row + archived plans persist. This lists those past relationships so the coach
    can see who they used to train and **re-invite** them (reopening the link to a
    fresh ``pending_coach_invite`` the athlete sees on their training home), plus
    any such re-invites still awaiting a response. A coach surface, so a non-coach
    is routed to their training home (mirroring ``RosterView``).
    """

    template_name = "meso/relationship_history.html"

    def get(self, request, *args, **kwargs):
        if not _is_coach(request.user):
            return redirect("meso:athlete_home")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        history = presenters.relationship_history(self.request.user)
        ctx["active"] = "roster"
        ctx["past"] = history["past"]
        ctx["reconnecting"] = history["reconnecting"]
        ctx["is_empty"] = not history["past"] and not history["reconnecting"]
        return ctx


class TemplateLibraryView(LoginRequiredMixin, TemplateView):
    """The coach's template library (``/meso/templates/``) — parity plan §3.4.

    A template = a ``Plan`` with ``is_template=True`` and an ``owner`` (no
    athlete), imported via ``meso_import_template`` or authored in the designer.
    This lists every template the requester owns, alphabetical, each opening in
    the same designer grid. When the coach has active clients, each row offers
    "Start for client" (``template_use`` — a live working copy) and "Batch
    deliver" (``plan_batch_deliver`` — a delivered copy per picked client).
    Login-gated (like ``DeliverView``): the library is scoped to the requester's
    own templates, so an anonymous visitor is bounced to login.
    """

    template_name = "meso/template_library.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "roster"
        ctx["templates"] = (
            Plan.objects.filter(is_template=True, owner=self.request.user)
            .annotate(
                block_count=Count("mesocycles", distinct=True),
                week_count=Count("mesocycles__weeks", distinct=True),
            )
            .order_by("title")
        )
        # The coach's deliverable clients, offered as "Start for client" /
        # "Batch deliver" targets. Soft-suspended (over-seat-limit, D6) links are
        # omitted — the endpoints re-check, this just keeps the screen honest.
        ctx["clients"] = [
            {"id": rel.pk, "name": rel.athlete.display_name()}
            for rel in CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .exclude(pk__in=billing_access.suspended_athlete_ids(self.request.user))
            .select_related("athlete")
            .order_by("athlete__name", "athlete__email")
        ]
        return ctx


class AthleteProfileView(LoginRequiredMixin, TemplateView):
    """Full athlete record — only viewable by a coach with an active link."""

    template_name = "meso/athlete_profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        link = (
            CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .select_related("athlete", "athlete__athlete_profile")
            .prefetch_related("athlete__contraindications")
            .filter(athlete_id=kwargs["pk"])
            .first()
        )
        if link is None:
            raise Http404("Unknown athlete")
        # #441 P1-2: the button-less "profile" tour step completes on the
        # visit itself — advance the tour if this coach is parked on it.
        meso_tour.advance_if_on_step(self.request.user, "profile")
        ctx["active"] = "roster"
        # The relationship's working program (first-time-UX Phase 1): when one
        # exists the CTAs open it in the designer; when not, they create one.
        working_plan = link.working_plan()
        ctx["working_plan"] = working_plan
        # Light up the program block (cadence, the macrocycle rail, status,
        # latest session). The athlete identity record carries the program
        # overlay merged in.
        athlete = presenters.profile_athlete(link.athlete)
        program = presenters.profile_program(link, working_plan)
        athlete.update(program["athlete"])
        ctx["athlete"] = athlete
        ctx["macrocycle"] = program["macrocycle"]
        ctx["results_summary"] = program["results_summary"]
        # The athlete's standing bests, in this link's plan unit (Phase 4d).
        ctx["personal_records"] = presenters.coach_personal_records(link)
        ctx["coach_style"] = presenters.coach_style(self.request.user)
        # Whether to offer "Draft with AI" on the create CTA — the same agent
        # allowance gate the endpoint enforces (the draft *is* an agent run).
        ctx["can_use_agent"] = billing_access.can_use_agent(self.request.user)
        return ctx


class UsageDashboardView(UserPassesTestMixin, TemplateView):
    """Owner-facing agent usage + margin dashboard (agent-usage Phase 4).

    A **staff-gated**, all-coach view of the per-month usage report that Phases 1–3
    capture, aggregate (``build_report``), and alert on (``margin_alerts``) — the
    web read-out the ``meso_agent_usage_report`` command renders as text. Not
    coach-scoped: it's the operator's cost/margin view across the whole tenant.

    Gate: an anonymous visitor bounces to login (``UserPassesTestMixin`` default);
    an authenticated non-staff user gets a flat 403 (``handle_no_permission``), so
    a logged-in coach can't probe org-wide spend.
    """

    template_name = "meso/usage_dashboard.html"

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        # Authenticated-but-unauthorized → 403 (not a pointless login bounce);
        # anonymous → the mixin's login redirect.
        if self.request.user.is_authenticated:
            raise PermissionDenied
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = self._window()
        report = usage_report.build_report(start=start, end=end)
        threshold = usage_report.resolve_alert_threshold()
        ctx["active"] = "usage"
        ctx.update(presenters.usage_dashboard(report, threshold=threshold))
        return ctx

    def _window(self):
        """The report window from ``?month=YYYY-MM``; current month on bad input.

        A hand-edited or malformed ``month`` degrades to the current month with a
        flashed warning rather than erroring, so the page always renders.
        """
        raw = self.request.GET.get("month")
        if raw:
            try:
                year, month = usage_report.parse_month(raw)
            except ValueError:
                messages.error(
                    self.request,
                    f"Ignoring invalid month {raw!r}; showing the current month.",
                )
            else:
                return usage_report.month_bounds(year, month)
        return usage_report.current_month_bounds()


class TourFunnelView(UserPassesTestMixin, TemplateView):
    """Owner-facing guided-tour funnel dashboard (#441 P3-6).

    The staff read-out of the ``TourEvent`` funnel: per-kind totals, the
    per-variant (sandbox vs. self) breakdown, the per-advance-step table, and a
    Started → Opt-in → Completed funnel — the web complement to reading the raw
    rows in the admin. Aggregation lives in ``presenters.tour_funnel``.

    Gate mirrors ``UsageDashboardView`` exactly: anonymous bounces to login
    (``UserPassesTestMixin`` default); an authenticated non-staff user gets a
    flat 403, so a logged-in coach can't probe org-wide tour analytics.

    Optional ``?variant=sandbox|self`` narrows to one audience and ``?days=N``
    to a trailing window; the default is all-time, all-variants.
    """

    template_name = "meso/tour_funnel.html"

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "tour_funnel"
        variant = self.request.GET.get("variant")
        if variant not in ("sandbox", "self"):
            variant = None
        since = None
        days = None
        raw_days = self.request.GET.get("days")
        if raw_days:
            try:
                parsed = int(raw_days)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                # Cap to ~10 years so an enormous value can't OverflowError the
                # timedelta; only surface ``days`` once a real window applies, so
                # the heading never claims to show "last abc days".
                parsed = min(parsed, 3650)
                since = timezone.now() - datetime.timedelta(days=parsed)
                days = parsed
        ctx["variant"] = variant
        ctx["days"] = days
        ctx.update(presenters.tour_funnel(variant=variant, since=since))
        return ctx


class ProductAnalyticsView(UserPassesTestMixin, TemplateView):
    """Owner-facing product-analytics dashboard (#509 slice 2).

    The staff read-out of first-party ``Event`` usage plus Meso's existing
    tables: active users, the invite→delivery→log activation funnel, feature
    adoption, and Meso's own transactional email — the web complement to
    querying ``Event`` directly in the admin. Aggregation lives in
    ``presenters.product_analytics``.

    Gate mirrors ``TourFunnelView``/``UsageDashboardView`` exactly: anonymous
    bounces to login (``UserPassesTestMixin`` default); an authenticated
    non-staff user gets a flat 403.

    ``?days=7|30|90`` picks the report window (default 30; anything else
    degrades to 30 with a flashed warning), parsed exactly like
    ``notifications.EmailDashboardView._days``.
    """

    template_name = "meso/product_analytics.html"
    WINDOW_DAYS = (7, 30, 90)

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        days = self._days()
        ctx.update(presenters.product_analytics(days=days))
        ctx["active"] = "analytics"
        ctx["window_options"] = self.WINDOW_DAYS
        ctx["email_dashboard_url"] = (
            reverse("notifications:email_dashboard") + f"?days={days}"
        )
        return ctx

    def _days(self):
        """The report window (in days) from ``?days=``; 30 on bad/missing input."""
        raw = self.request.GET.get("days")
        if raw:
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                parsed = None
            if parsed in self.WINDOW_DAYS:
                return parsed
            messages.error(
                self.request,
                f"Ignoring invalid days {raw!r}; showing the last 30 days.",
            )
        return 30


class CoachBillingView(LoginRequiredMixin, TemplateView):
    """Coach-facing billing & plan page (agent-usage — coach surface).

    A coach's own plan/tier, the bill they owe (base + per active seat), the
    upgrade CTAs, and their AI-agent runs this month broken down per athlete
    — the coach-scoped complement to the staff-only owner usage dashboard (which
    shows org-wide *cost*). A coach never sees the internal cost estimate here, only
    what they pay and how much they've used (``presenters.coach_billing``).

    Gate: anonymous → login (``LoginRequiredMixin``); a pure athlete (no coach
    signal) is routed to their training home, mirroring the roster's role split, so
    a non-coach never lands on an empty billing surface.
    """

    template_name = "meso/coach_billing.html"

    def get(self, request, *args, **kwargs):
        if not _is_coach(request.user):
            return redirect("meso:athlete_home")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "billing"
        ctx.update(
            presenters.coach_billing(
                self.request.user,
                checkout_pending=_checkout_pending(self.request),
            )
        )
        return ctx


def _reserve_plan_draft(request, plan):
    """Reserve an agent run + a drafting batch to draft ``plan``, or ``None``.

    Mirrors ``agent_propose``'s metering: lock the coach row, check the agent
    allowance, and create the batch inside the caller's transaction so concurrent
    reservations serialize (the batch table is the run ledger). Returns the batch
    to dispatch, or ``None`` — with a flash — when the draft can't run (allowance
    exhausted, or no API key). On ``None`` the plan is still created blank so the
    coach can build it by hand. Must be called within a transaction.

    Sandbox gate (S3): a sandbox coach never drafts — the plan is still built,
    just blank, same as the no-API-key path.
    """
    if meso_sandbox.is_sandbox(request.user):
        return None
    User.objects.select_for_update(no_key=True).filter(pk=request.user.pk).first()
    if not billing_access.can_use_agent(request.user):
        messages.info(request, DRAFT_ALLOWANCE_MESSAGE)
        return None
    if agent_client.get_default_client() is None:
        messages.info(
            request,
            "The AI agent isn't configured here, so your program starts blank.",
        )
        return None
    messages.success(
        request,
        "Drafting your program with the AI agent — review the proposed week in a "
        "moment.",
    )
    # §4b: a freshly scaffolded plan (``Plan.scaffold``) has exactly one block,
    # so "the block the coach had open" is unambiguous here — no ``mesocycle_id``
    # to parse, unlike ``agent_propose``'s existing-plan path.
    return agent_service.create_drafting_batch(
        plan,
        agent_service.DRAFT_INSTRUCTION,
        coach=request.user,
        mesocycle=_default_grid_mesocycle(plan),
        trigger=AgentProposalBatch.Trigger.DRAFT,
    )


@login_required
@require_POST
def plan_create(request, pk):
    """Create (or open) an individual program for one of the coach's athletes.

    The action behind the
    "+ New program" / "Build a program" CTAs (first-time-UX Phase 1). Coach-scoped
    to an *active* link (a foreign, pending, or unknown athlete is a flat 404).
    Idempotent: reuses the relationship's existing non-archived plan, only
    creating (with a starter scaffold) when there is none, under a row lock so two
    concurrent submits can't each create one. Billing (D6): a soft-suspended
    athlete (an over-limit coach's newer relationships) is frozen — a flashed
    redirect, no plan created — consistent with the edit gate the designer would
    hit immediately. Lands in the designer.

    With ``draft`` set (the "Draft with AI" CTA), a *freshly-created* scaffold is
    handed to the agent to draft the first week (Q2 fast-follow); the proposal
    lands in the review gate. The draft only fires on a new plan — never
    overwriting an existing program — and is metered like the manual agent run.

    Also the "designer"/"agent" steps' self-variant data action (guided-tour
    Phase 3) — same ``tour=1``-gated funnel opt-in as ``roster_add_self`` (this
    endpoint is hit organically far more often, from every real "+ New
    program"/"Build a program" CTA, so the marker is what keeps those from
    being miscounted). ``draft`` doubles as which of the two tour steps fired
    it: the "agent" step always sends ``draft=agent``, "designer" never does.
    """
    draft = bool(request.POST.get("draft"))
    draft_batch = None
    with transaction.atomic():
        relationship = (
            CoachAthlete.objects.select_for_update()
            .for_coach(request.user)
            .active()
            .filter(athlete_id=pk)
            .first()
        )
        if relationship is None:
            raise Http404("Unknown athlete")
        # Per-athlete freeze (D6): a suspended relationship can't be edited, so
        # don't let one spawn a plan the autosave/deliver endpoints would 402 on.
        if relationship.pk in billing_access.suspended_athlete_ids(request.user):
            messages.error(request, OVER_LIMIT_MESSAGE)
            return redirect("meso:athlete", pk=pk)
        existing = relationship.working_plan()
        plan = existing or relationship.create_plan()
        if draft and existing is None:
            draft_batch = _reserve_plan_draft(request, plan)
    # analytics (#509): only a freshly-created plan counts — reopening an
    # existing one on a second POST is not a new "plan created" action.
    if existing is None:
        track(
            EventName.PLAN_CREATED,
            actor=request.user,
            subject=plan,
            athlete=str(relationship.athlete_id),
            draft=draft,
            demo=relationship.is_demo,
        )
    # Dispatch (and bump the plan) outside the lock, mirroring ``agent_propose``.
    if draft_batch is not None:
        agent_jobs.dispatch_proposal(draft_batch.pk)
        _touch_plan(plan)
        track(
            EventName.AGENT_PROPOSAL_RUN,
            actor=request.user,
            subject=draft_batch,
            trigger=draft_batch.trigger,
            demo=relationship.is_demo,
        )
    # The tour marker (``tour=1``) picks the step from ``draft`` ("agent" vs
    # "designer"). #441 P3-2 also counts the organic twin while touring — but
    # only when this POST's action shape (``draft`` → agent, plain → designer)
    # matches the step the coach is parked on, so a manual "New program" on the
    # agent step isn't miscounted as an AI-draft opt-in. One ``record_opt_in``
    # call, never double-recorded.
    natural_step = "agent" if draft else "designer"
    if request.POST.get("tour") == "1":
        tour_step = natural_step
    elif (
        meso_tour.variant_for(request.user) == "self"
        and meso_tour.current_step_key(request.user) == natural_step
    ):
        # Organic twin, self variant only: the sandbox opt-in path is
        # demo_load(segment=program), not plan_create, so a sandbox coach's
        # organic "+ New program" must not log a self-variant plan_create opt-in.
        tour_step = natural_step
    else:
        tour_step = None
    if tour_step is not None:
        meso_tour.record_opt_in(request.user, "self", tour_step, "plan_create")
    # #441 P3-5: both the designer and agent self steps complete on the same
    # signal — "the coach's own plan now exists" — regardless of which control
    # created it (a plain "+ New program" or "Draft with AI", the latter sending
    # draft=agent even while parked on designer). So advance is decoupled from
    # ``natural_step`` (which is only the funnel attribution above): advance
    # whichever of the two the coach is parked on — each call no-ops off its
    # step. Self variant only (the sandbox designer completes via
    # demo_load(program), not plan_create) and gated on the self-link so building
    # a program for another athlete the coach coaches never skips their own tour.
    if meso_tour.variant_for(request.user) == "self" and relationship.is_self:
        meso_tour.advance_if_on_step(request.user, "designer")
        meso_tour.advance_if_on_step(request.user, "agent")
    return redirect("meso:designer_plan", plan_id=plan.pk)


def _client_ip(request):
    """The visitor's IP for a ``SandboxSession`` — prod sits behind Caddy.

    Prefers the first hop of ``X-Forwarded-For`` (the original client, set by
    the reverse proxy); falls back to ``REMOTE_ADDR`` for a direct connection
    (local dev, tests).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _sandbox_rate_limited(ip):
    """Whether this IP has minted too many sandboxes in the rolling hour.

    Cache-counted: ``cache.add`` seeds the counter with its one-hour TTL (a
    no-op when it already exists), then ``incr`` bumps it — on Redis ``INCR``
    preserves the TTL, so the window rolls rather than resets. Counts
    *attempts*, so hammering past the limit never re-opens it early.
    """
    if not ip:
        return False  # unattributable (no proxy header, no REMOTE_ADDR)
    key = f"meso:sandbox:rate:{ip}"
    cache.add(key, 0, timeout=3600)
    count = cache.incr(key)
    return count > settings.MESO_SANDBOX_PER_IP_PER_HOUR


@require_GET
def sandbox_enter(request):
    """Public, no-signup entry into a throwaway coach sandbox (issue #389, S1).

    An anonymous visitor gets a fresh, populated sandbox coach (``sandbox.
    create_sandbox``) and is logged in as it — every existing login-gated view,
    CSRF token, and coach-scoping query then just works, no special-casing
    needed. An already-authenticated visitor (including one revisiting this URL
    mid-visit) is simply routed to the roster — the session cookie is the
    "resume", so no second sandbox is minted.

    Abuse bounds (Phase 2): every entry mints real DB rows, so creation is
    capped globally (``MESO_SANDBOX_MAX_CONCURRENT`` live sandboxes; the hourly
    expiry sweep frees slots) and per IP (``MESO_SANDBOX_PER_IP_PER_HOUR``,
    cache-counted). A bounded visitor gets a friendly flash and the landing
    page — no rows created. Every response carries ``X-Robots-Tag: noindex``:
    a GET that mints DB rows must not be crawled repeatedly.
    """

    def _noindex(response):
        response["X-Robots-Tag"] = "noindex"
        return response

    if request.user.is_authenticated:
        return _noindex(redirect("meso:roster"))
    if (
        SandboxSession.objects.count() >= settings.MESO_SANDBOX_MAX_CONCURRENT
        or _sandbox_rate_limited(_client_ip(request))
    ):
        messages.info(
            request,
            "The demo is busy right now — please try again in a little while.",
        )
        return _noindex(redirect("meso:roster"))
    user = meso_sandbox.create_sandbox(source_ip=_client_ip(request))
    # Two auth backends are configured (ModelBackend + allauth) — login() can't
    # infer which one, so it must be named explicitly.
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # No welcome flash: the persistent sandbox banner (_meso_base.html) already
    # says "You're in a live demo" on every screen, so a flash on entry only
    # duplicated it on the roster (issue #425). Carry-over at signup is deferred
    # (S6) — a new account starts a fresh workspace, and neither surface promises
    # kept work.
    return _noindex(redirect("meso:roster"))


#: Per-segment success flash, keyed the same as ``meso_demo.SEGMENTS``.
_SEGMENT_MESSAGES = {
    "athletes": "Sample athletes added — meet your new roster.",
    "program": "Sample program built — a full mesocycle, ready to explore.",
    "delivery": "This week delivered to Maya's phone.",
    "log": "Session logged — Maya's results are in.",
}


@login_required
@require_POST
def demo_load(request):
    """Load a coach-scoped demo workspace so a new coach can explore (Q3, Phase 2).

    A populated, **clearly-labeled, fully-removable** workspace — five athletes and
    a built/delivered/logged program — scoped to this coach, idempotent,
    billing-neutral, and silent (no demo-athlete email/push). Lands on the roster
    where the data now shows, with a "Remove demo data" affordance.

    An optional ``segment`` POST field narrows the load to one slice of the demo
    (``meso_demo.SEGMENTS`` — ``athletes``/``program``/``delivery``/``log``)
    instead of the full aggregate; an unrecognized name loads nothing and 400s.
    No URL changes: this stays the one ``meso:demo_load`` endpoint for both the
    full load and (guided-tour Phase 2) each step's per-segment "add sample data"
    action — the tour passes ``segment`` here.

    An optional ``next`` POST field sends the user back where they came from
    (guided-tour Phase 2): the tour's segment forms fire from mid-tour pages
    (designer, deliver, ...) and always landing on the roster would teleport
    the user away from the step they're on. Only a safe local path is honored
    (leading ``/``, not scheme-relative ``//``, and passing Django's
    ``url_has_allowed_host_and_scheme``); anything else — including the
    existing roster/tour-skip callers, which don't send ``next`` at all —
    falls back to the roster exactly as before.
    """
    # Loading a demo is an implicit "I'm coaching now": ensure the CoachProfile
    # exists (mirrors start_coaching's free path) so demo links never make a user a
    # coach via a side door without one — keeping coach state consistent, for both
    # the aggregate and per-segment paths.
    CoachProfile.objects.get_or_create(user=request.user)
    segment = request.POST.get("segment")
    if segment:
        loader = meso_demo.SEGMENTS.get(segment)
        if loader is None:
            return HttpResponseBadRequest("Unknown demo segment.")
        loader(request.user)
        messages.success(request, _SEGMENT_MESSAGES[segment])
        # Phase 4 funnel event (#430): a per-segment opt-in. No ``tour=1``
        # marker needed here (unlike roster_add_self/plan_create below) — the
        # ``segment`` field is only ever sent by the sandbox tour's own
        # per-step forms (``meso_tour.js``'s ``segment`` action branch); no
        # other UI in the app posts it, so every call here really is the
        # tour's sandbox variant.
        meso_tour.record_opt_in(
            request.user, "sandbox", meso_tour.step_key_for_segment(segment), segment
        )
        # #441 P3-5: the sandbox action steps auto-advance the moment their
        # segment loads — the coach doesn't have to click Next after doing the
        # thing. A no-op unless parked exactly on the step this segment offers.
        meso_tour.advance_if_on_step(
            request.user, meso_tour.step_key_for_segment(segment)
        )
    else:
        meso_demo.load_demo(request.user)
        messages.success(
            request,
            "Demo data loaded — explore a populated workspace. Remove it any time.",
        )
    next_path = request.POST.get("next", "")
    if (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and url_has_allowed_host_and_scheme(next_path, allowed_hosts=None)
    ):
        return redirect(next_path)
    return redirect("meso:roster")


@login_required
@require_POST
def demo_clear(request):
    """Remove exactly this coach's demo data (never their real data) — the teardown.

    Removing the demo data a mid-flight tour was walking you through would leave
    the step index parked on a now-empty workspace (e.g. the profile step with
    no athlete to open), so an actively-touring coach is restarted at step 0
    (#441 P2-5b). A dismissed/completed tour is left alone — only a live tour
    is out of sync with the cleared workspace.
    """
    meso_demo.clear_demo(request.user)
    if meso_tour.is_touring(request.user):
        profile = CoachProfile.objects.get(user=request.user)
        meso_tour.start_tour(profile)
    messages.success(request, "Demo data removed.")
    return redirect("meso:roster")


@login_required
@require_POST
def roster_add_self(request):
    """Put the coach on their own roster as an athlete (guided-tour Phase 0).

    Self-coaching: the link goes straight to ``active`` (no invite dance) and is
    never a paid seat (``is_self`` is excluded from ``billable()``), so there's
    no ``can_add_athlete`` gate here — mirroring the demo loader. Idempotent:
    re-posting reuses the one self-link ``unique(coach, athlete)`` allows.

    This is also the "welcome" step's self-variant data action (guided-tour
    Phase 3), but it's hit organically too (roster.html's own standing "Add
    yourself" affordance) — a Phase 4 funnel opt-in event only fires when the
    POST carries the tour driver's ``tour=1`` marker field (``meso_tour.js``
    adds it to every action form it builds), so the organic path isn't
    miscounted as tour engagement.
    """
    with transaction.atomic():
        # LOCK ORDER (#596) — a self-link has two User parents that happen to
        # be the same row. Reserve that parent before CoachProfile/add_self can
        # insert children, matching User-rooted cascade deletes.
        locked_coach = (
            User.objects.select_for_update(no_key=True)
            .filter(pk=request.user.pk)
            .first()
        )
        if locked_coach is None:
            raise Http404("Unknown coach")
        # Like demo_load: adding yourself is an implicit "I'm coaching now", so
        # make sure the CoachProfile exists rather than minting a coach via a
        # side door.
        CoachProfile.objects.get_or_create(user=request.user)
        CoachAthlete.add_self(request.user)
    messages.success(
        request,
        "You're on your roster — build a program for yourself like any athlete.",
    )
    # The tour marker (``tour=1``) counts an in-tour opt-in; #441 P3-2 also counts
    # the organic twin when the coach is actively touring & parked on the matching
    # (welcome) step. The organic fallback is self-variant only — a sandbox coach
    # opts in via demo_load, not roster_add_self. One call, never double-recorded.
    if request.POST.get("tour") == "1" or (
        meso_tour.variant_for(request.user) == "self"
        and meso_tour.current_step_key(request.user) == "welcome"
    ):
        meso_tour.record_opt_in(request.user, "self", "welcome", "roster_add_self")
    # #441 P3-5: the welcome step auto-advances once the coach is on their own
    # roster — no manual Next needed. Self variant only (the sandbox welcome
    # completes by loading demo athletes, not by adding a self-link). A no-op
    # unless parked on welcome.
    if meso_tour.variant_for(request.user) == "self":
        meso_tour.advance_if_on_step(request.user, "welcome")
    return redirect("meso:roster")


@login_required
@require_POST
def tour_state(request):
    """Advance/back/goto/dismiss/complete/restart the guided demo tour (#430, Phase 2).

    Persists on the requesting coach's ``CoachProfile.tour_state``
    (get_or_create, mirroring ``demo_load``/``roster_add_self`` — driving the
    tour is itself an implicit "I'm coaching now"). The front-end driver calls
    this via ``fetch`` and gets the new state back as JSON; a bare form POST
    (no ``X-Requested-With``, e.g. JS-disabled) degrades to a redirect back to
    the roster instead.

    Phase 4 (analytics + polish, #430) records the funnel event alongside each
    transition: "advance"/"goto" only counts as a step **advanced** when the
    resulting step is actually further along (the driver posts "goto" for both
    Back and Next — see ``meso_tour.js``'s ``goTo`` — so a backward jump or an
    already-clamped no-op records nothing); "dismiss" and "complete" record on
    the step they fired from/landed on; "restart" records a fresh **started**.
    The variant is read once via ``variant_for`` since any of these can come
    from either audience.
    """
    profile, _ = CoachProfile.objects.get_or_create(user=request.user)
    action = request.POST.get("action")
    current_step = (profile.tour_state or {}).get("step", 0)
    variant = meso_tour.variant_for(request.user)

    if action == "advance":
        meso_tour.set_step(profile, current_step + 1)
        if profile.tour_state["step"] > current_step:
            meso_tour.record_advanced(
                request.user,
                variant,
                meso_tour.STEPS[profile.tour_state["step"]]["key"],
            )
    elif action == "back":
        meso_tour.set_step(profile, current_step - 1)
    elif action == "goto":
        meso_tour.set_step(profile, request.POST.get("step", current_step))
        if profile.tour_state["step"] > current_step:
            meso_tour.record_advanced(
                request.user,
                variant,
                meso_tour.STEPS[profile.tour_state["step"]]["key"],
            )
    elif action == "dismiss":
        meso_tour.dismiss(profile)
        meso_tour.record_dismissed(
            request.user, variant, meso_tour.STEPS[current_step]["key"]
        )
    elif action == "complete":
        meso_tour.complete(profile)
        meso_tour.record_completed(request.user, variant)
    elif action == "restart":
        meso_tour.start_tour(profile)
        meso_tour.record_started(request.user, variant)
    else:
        return HttpResponseBadRequest("Unknown tour action.")

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(profile.tour_state)
    return redirect("meso:roster")


@login_required
@require_POST
def tour_skip(request):
    """The O6 "skip · load everything" shortcut: the full aggregate demo, tour marked done.

    Reuses the exact pre-tour ``demo_load`` behavior (the whole workspace, one
    shot) for the **sandbox** variant only, and always marks the tour
    ``completed`` so it doesn't resurface on the next page load — the tour is
    meant to be a helpful default, never a wall (O6). Records a **skipped**
    funnel event (not **completed** — distinct from actually walking the tour
    to the end, even though both leave ``tour_state`` parked on the same
    ``completed`` status), keyed to whichever step the coach skipped from.
    """
    profile, _ = CoachProfile.objects.get_or_create(user=request.user)
    current_step = (profile.tour_state or {}).get("step", 0)
    variant = meso_tour.variant_for(request.user)
    # Only the anonymous sandbox loads the fake demo workspace; a real
    # (self-variant) coach must never get fake athletes on their live
    # roster (#441 P1-1, O5) — for them "skip" just ends the tour.
    if variant == "sandbox":
        meso_demo.load_demo(request.user)
        messages.success(
            request,
            "Demo data loaded — explore a populated workspace. Remove it any time.",
        )
    meso_tour.complete(profile)
    meso_tour.record_skipped(
        request.user, variant, meso_tour.STEPS[current_step]["key"]
    )
    return redirect("meso:roster")


@login_required
@require_GET
def tour_config(request):
    """Read-only snapshot of the coach's authoritative tour config (issue #451).

    The self-variant deliver/results steps take their data-producing action via
    ``fetch`` (delivering the coach's own block / logging their own session) —
    no page reload — so the server advances ``tour_state`` (via
    ``advance_self_step_if_complete`` in ``plan_deliver``/``athlete_log_session``)
    but the already-mounted ``meso_tour.js`` card can't see it until the coach
    next navigates. This endpoint hands the driver the *same* ``build_config``
    the roster template embeds via ``json_script`` so it can re-read the
    authoritative ``tour_state`` and re-render at whatever step the server now
    reports.

    Advance stays server-authoritative — the ``TourEvent`` funnel and the resume
    state both live in ``tour_state``, written only at the tour's own POST
    endpoints; the client just mirrors them here, never advancing locally. A
    plain read, so ``@require_GET`` (a state change would be a POST). Returns
    ``{}`` for a coach with no ``CoachProfile`` (``build_config`` returns
    ``None`` there — the driver treats an empty/steps-less config as "no tour"
    and simply leaves the card where it is).
    """
    variant = meso_tour.variant_for(request.user)
    config = meso_tour.build_config(request.user, variant)
    return JsonResponse(config if config is not None else {})


@require_GET
def sandbox_signup(request):
    """The sandbox's conversion hop into a real account (issue #389, S1).

    allauth bounces an already-authenticated visitor away from
    ``/accounts/signup/``, so a sandbox coach must be logged out first — the
    sandbox ``User`` row is left in place for the Phase 2 expiry sweep to reap,
    never carried into the new account (S6: deferred carry-over). A
    non-sandbox authenticated visitor is just sent along too (harmless).

    ``next`` targets the become-a-coach funnel, not the roster: a brand-new
    signup has no ``CoachProfile``, so ``RosterView`` would route them to the
    athlete home — the wrong surface for someone converting to run the AI
    agent. ``BecomeCoachView`` handles both arrivals: a fresh non-coach gets
    the start-coaching form (whose POST creates the ``CoachProfile``), and an
    existing coach is sent on to the roster.
    """
    if meso_sandbox.is_sandbox(request.user):
        logout(request)
    query = urlencode({"next": reverse("meso:become_coach")})
    return redirect(f"{reverse('account_signup')}?{query}")


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's own logged-in surface, distinct from the coach's view of an
# athlete (``/meso/athlete/<uuid>/``). Everything is scoped to the athlete's
# *active* coaches (``for_athlete``) and to non-archived plans. Edits are live
# (2d, parity plan §3.3): the athlete sees the plan exactly as it stands the
# moment the coach types it — delivery is a one-time heads-up + snapshot, never
# a visibility gate. An out-of-scope session is a flat 404 — never a silent
# empty render.


def _pwa_context():
    """Push install config for the athlete templates (Phase 4b — S7).

    ``push_enabled`` gates the subscribe affordance + VAPID key in the template;
    with no keys configured the PWA still installs and logs offline, it just
    won't offer push.
    """
    return {
        "push_enabled": meso_push.push_enabled(),
        "vapid_public_key": meso_push.vapid_public_key(),
    }


def _athlete_plans(user):
    """Plans the athlete may see: active-coach, non-archived (D-a)."""
    return Plan.objects.for_athlete(user).exclude(status=Plan.Status.ARCHIVED)


def _athlete_has_completed_log(user):
    """Whether the athlete has ever *completed* a session log (Phase 4).

    Drives the one-time first-log coachmark: it's a *first*-log nudge, so once
    they've finished a real session (in any plan) they know how — the hint hides.
    Gated on a ``done`` log specifically (not any row): a "Save progress" draft
    writes a ``pending`` log while the session still reads "To do", and the hint
    teaches that final "Log session" step, so a draft must not suppress it.
    Server-driven, so the nudge is naturally one-time + cross-device with no
    per-device flag or migration; it vanishes the moment the first log lands.
    """
    return SessionLog.objects.filter(
        athlete=user, status=SessionLog.Status.DONE
    ).exists()


def _athlete_session_or_404(user, pk):
    """A session the athlete owns, or ``Http404``.

    404 unless the session's plan is one the athlete reaches through an active
    coach link — a foreign athlete, an archived plan, or an unknown id are
    indistinguishable (no leak). Delivery is NOT checked (2d): edits are live,
    so an undelivered week's sessions are as loggable as any other.

    Soft delete (designer framework Phase 0): a session the coach removed —
    or one under a removed week — is gone from the athlete's surface too.
    Cells are read live via ``session.cells()``
    (P0 fixed-lineup cutover), so every downstream call (the logger grid,
    ``_clean_logged_sets``'s allowed ids, ``athlete_set_one_rm``) sees only
    live rows. Already-logged history is untouched — those reads go through
    ``SessionLog``/``LoggedSet``, never this lookup.
    """
    session = (
        Session.objects.filter(
            pk=pk,
            week__mesocycle__plan__in=_athlete_plans(user),
            deleted_at__isnull=True,
            week__deleted_at__isnull=True,
        )
        .select_related("week__mesocycle__plan__relationship")
        .first()
    )
    if session is None:
        raise Http404("Unknown session")
    return session


def _is_prefetch(request):
    """Whether the browser is fetching this speculatively rather than showing it.

    A prefetch, a prerender or an iOS link preview is a real cookied GET, so
    nothing else here can tell it apart from a tap — but a browser doing one
    announces it: ``Sec-Purpose: prefetch`` (fetch metadata, current Chrome
    and Firefox) or the older ``Purpose: prefetch`` (older Chrome, Safari's
    preview). Used to keep a link the athlete never tapped from burning a
    push notification's one-shot ``clicked_at`` (#509 slice 3).
    """
    purpose = (
        f"{request.headers.get('sec-purpose', '')} {request.headers.get('purpose', '')}"
    )
    return "prefetch" in purpose.lower() or "prerender" in purpose.lower()


class AthleteHomeView(LoginRequiredMixin, TemplateView):
    """The athlete's training home: their live programs, free navigation.

    The app never asserts a "you are here" position (docs/meso/remove-
    current-week-plan.md): a card opens onto a derived scroll hint (the last
    week with any of the athlete's own logged sets, else the earliest live
    week — see ``presenters.athlete_home``), and ``?week=<id>`` is the ONLY
    week selector — a display-only override (e.g. tapping a week chip) that
    picks which week of its block a card shows, nothing more. A missing/
    invalid id is just ``None``, which renders exactly like a bare request.
    """

    template_name = "meso/athlete_home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "training"
        # push_clicked (#509 slice 3): the block-delivered push deep-links
        # here carrying its ledger id. Only a real GET counts — Django routes
        # HEAD through get() (and so through this context build) too, and a
        # browser that prefetches or previews the link announces itself
        # (`Sec-Purpose: prefetch` per the fetch metadata spec, `Purpose:
        # prefetch` from older Chrome and Safari's link preview). A tap the
        # athlete never made must not burn the row's one-shot clicked_at.
        if self.request.method == "GET" and not _is_prefetch(self.request):
            record_push_click(self.request)
        try:
            focus_week_id = int(self.request.GET.get("week", ""))
        except (TypeError, ValueError):
            focus_week_id = None
        ctx["plans"] = presenters.athlete_home(
            self.request.user, focus_week_id=focus_week_id
        )
        # Pending coach links (N4 Phase 2): invites awaiting my reply + requests
        # I've sent + the request-a-coach form all live on this surface.
        ctx["pending"] = presenters.athlete_pending(self.request.user)
        # The athlete's standing bests, in their current plan's unit (Phase 4d).
        ctx["personal_records"] = presenters.athlete_personal_records(self.request.user)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        # First-log coachmark (Phase 4): only when there's a session to tap
        # *and* the athlete has never logged — pointing "tap a session below"
        # at an empty week would be noise.
        has_sessions = any(card["sessions"] for card in ctx["plans"])
        ctx["show_first_log_hint"] = has_sessions and not _athlete_has_completed_log(
            self.request.user
        )
        ctx.update(_pwa_context())
        return ctx


class AthleteSessionView(LoginRequiredMixin, TemplateView):
    """One session — the athlete's interactive logger (Phase 2).

    Renders the prescribed grid as set-input rows pre-filled from the athlete's
    own existing log, and injects ``log_data`` for the Alpine logger to hydrate
    from and POST back to ``athlete_log_session``.
    """

    template_name = "meso/athlete_session.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = _athlete_session_or_404(self.request.user, kwargs["pk"])
        # session_opened analytics (#509): GET only — Django routes HEAD
        # through ``get()`` (and so this same context build) too.
        if self.request.method == "GET":
            track(EventName.SESSION_OPENED, actor=self.request.user, subject=session)
        sess = presenters.athlete_session(session, self.request.user)
        ctx["active"] = "training"
        ctx["session"] = sess
        ctx["log_data"] = presenters.athlete_log_payload(sess)
        # Whose offline queue this page may flush (#527). The queue lives in
        # localStorage, which outlasts a logout: without this, the next athlete
        # to sign in on the device would replay the last one's writes under
        # their own login, and a line refused as "not your session" is dropped.
        ctx["log_data"]["owner"] = str(self.request.user.pk)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        # First-log coachmark (Phase 4): teach the logger only to a first-ever
        # logger — any prior log means they already know how.
        ctx["show_first_log_hint"] = not _athlete_has_completed_log(self.request.user)
        ctx.update(_pwa_context())
        return ctx


# Free-form text cells per logged set, mapped to their model ``max_length``.
LOG_SET_FIELDS = {"reps": 32, "load": 32, "rpe": 32}
# A client-minted ``client_id`` (#567) is never stored — it only round-trips
# through one request/response pair — so this just bounds the noise a bad
# client can put in a 400 error message and, transitively, in the payload
# itself; there's no column length to mirror.
MAX_CLIENT_ID_LENGTH = 64


@login_required
@require_POST
def athlete_log_session(request, pk):
    """Upsert the athlete's log for a session they own (Phase 2).

    Replaces the athlete's own ``SessionLog`` + ``LoggedSet`` rows for this
    session with the posted state, flips the session done (unless an explicit
    ``status`` says otherwise; a DONE log is never downgraded, see below), and
    stamps the date (today when none is given).
    Scoped by ``_athlete_session_or_404`` — a foreign, archived, or
    unknown session is a flat 404, never a silent write. The body is fully
    validated *before* any write, so a bad request is a 400 that persists
    nothing; the write itself is idempotent (re-logging updates the one log,
    replacing its set rows rather than appending). These are the first real rows
    ``serialize_recent_logs`` grounds the agent on.
    """
    session = _athlete_session_or_404(request.user, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    status = payload.get("status", SessionLog.Status.DONE)
    if status not in (SessionLog.Status.PENDING, SessionLog.Status.DONE):
        return HttpResponseBadRequest("status must be 'pending' or 'done'.")

    # An explicit date is honored; a missing one defaults to today only when
    # *creating* the log — re-saving an existing log without a date keeps its
    # original date so editing a set days later doesn't move the workout (which
    # would reorder recent-log grounding). ``explicit_date`` is None when none
    # was sent.
    raw_date = payload.get("date")
    explicit_date = None
    if raw_date not in (None, ""):
        if not isinstance(raw_date, str):
            return HttpResponseBadRequest("date must be an ISO date string.")
        try:
            explicit_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return HttpResponseBadRequest("date must be an ISO date (YYYY-MM-DD).")

    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        return HttpResponseBadRequest("notes must be a string.")

    cleaned_sets, error = _clean_logged_sets(payload.get("sets", []), session)
    if error is not None:
        return error
    # #567: row identity. A payload is IDENTIFIED when every set names the row
    # it means — its own ``id`` (rendered by a previous save) or a fresh
    # ``client_id`` (a grid row with no server row yet) — rather than leaving
    # the server to infer it from ``(prescription, set_number)``, which is
    # evidence about a render that can be several saves old once a row is
    # hidden. Whole-payload, not per-row: a real client tags every set it
    # knows how to (it always knows, once it's on this contract), so a mixed
    # payload can only mean a legacy/stale-tab client that tags none of them —
    # there is no partial-trust story here, only "trust the ids" or "fall back
    # to the old guess for everything". Vacuously ``True`` for an empty list:
    # nothing differs between the two modes when there's nothing to match.
    identified = all(
        cs["id"] is not None or cs["client_id"] is not None for cs in cleaned_sets
    )
    plan = session.week.mesocycle.plan

    with transaction.atomic():
        # LOCK ORDER (#588) — every athlete write takes Plan before Session.
        # A restore or pre-locked cascade delete therefore finishes first; the
        # re-reads below then see a coherent surviving tree or fail cleanly.
        locked_plan = (
            Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        )
        if locked_plan is None:
            return HttpResponseNotFound("Unknown session")
        # Same lock `_upsert_parsed_set` takes, and it has to be BOTH sides to
        # work: `(session, athlete)` has no uniqueness, so an athlete who types
        # into a cell and immediately taps Save can have the blur POST and this
        # one both find no log and each create one. Locking only the blur path
        # leaves that race wide open. One workout split across two logs loses
        # the older one's sets from every later read, which takes the newest.
        locked_session = (
            Session.objects.select_for_update(of=("self",))
            .filter(pk=session.pk)
            .first()
        )
        if locked_session is None:
            return HttpResponseNotFound("Unknown session")
        # The shared newest-log rule for one (session, athlete) pair — see
        # models.newest_session_logs for the ordering rationale, the full
        # list of reads that share it, and the reads that deliberately don't.
        log = newest_session_logs(session, request.user).first()
        if log is None:
            log = SessionLog(session=session, athlete=request.user)
        # set_logged / session_completed analytics (#509): captured right here,
        # before this save changes anything, so they describe the log's state
        # walking in.
        sets_before = log.sets.count() if log.pk else 0
        was_done = log.status == SessionLog.Status.DONE
        # Status is STICKY once DONE (5b, settle.py): a posted "pending" never
        # downgrades a DONE log. The client already intends this — "Save
        # progress" posts `markDone ? "done" : this.status`, the status the page
        # last saw — but once the settle sweep can finish a log server-side, a
        # tab left open across the settle (or an offline-queued save replayed
        # later) would post its stale "pending" and silently undo the settle.
        # Mirrors 5a's rule that a blur never downgrades a DONE log
        # (`_upsert_parsed_set`).
        if not (
            status == SessionLog.Status.PENDING and log.status == SessionLog.Status.DONE
        ):
            log.status = status
        # Bump on every save, regardless of status — this endpoint is always a
        # real athlete action (unlike a cell blur, which fires on every focus
        # change whether or not anything changed), so there is no "untouched"
        # case to filter out here.
        log.last_activity_at = timezone.now()
        if explicit_date is not None:
            log.date = explicit_date
        elif log.date is None:  # first save (or a log never dated) → stamp today
            log.date = timezone.localdate()
        # else: a re-save with no date keeps the existing workout date.
        log.notes = notes
        log.save()
        # Replace only the rows the logger can re-post: sets whose prescription
        # cell is TRAINABLE in this session (``session.trainable_cells()`` — live
        # and non-skipped, the exact set the logger renders). A set logged against
        # a since-deleted/hidden/skipped cell — or one orphaned by an old hard
        # delete — is history, not draft state; wiping it here would silently
        # destroy the athlete's record on their next save (e.g. a row the coach
        # marked skipped after the athlete already logged it).
        #
        # ``parsed_set_is_hidden`` scopes the delete to rows the logger can
        # actually see, which is exactly the set it can repost — see that
        # predicate for why the two must share one definition. A parsed set's
        # ``prescription`` is also a trainable line-0 cell, so an unscoped
        # delete would wipe every freeform-parsed set, while sparing them by
        # ``source_line__isnull=True`` would leave a reclaimed row undeleted and
        # let the client duplicate it (5a, plan §5, §6). Filtered in Python
        # because the test re-parses cell text, which SQL can't express.
        #
        # A VISIBLE parsed row is only replaceable when this request posted its
        # slot. Visibility is judged from the CURRENT cell text, but the payload
        # was composed from what the page rendered — and a coach rewriting the
        # source line in between flips a row from hidden to visible without the
        # athlete's open page ever learning it exists. Deleting on visibility
        # alone therefore let an ordinary "Save progress" destroy an earned set
        # nobody had asked to change: the replace covered a row the client never
        # held, so nothing reposted it. Posting the slot is the client's proof it
        # was actually looking at that row.
        #
        # The cost is the mirror case — a reclaimed set the athlete clears from
        # the logger now survives, because a cleared row and a row the client
        # never saw are the same empty payload. Keeping unasked-for work beats
        # destroying it (the same call ``prescription_skip`` makes), and the
        # athlete's own sub-line is unaffected either way.
        posted = {(cs["prescription_id"], cs["set_number"]) for cs in cleaned_sets}
        replaceable = []
        # #541: a row about to be replaced can be the near end of a reclaim
        # link that is still open — either a held VISIBLE parsed row (its own
        # `source_line` names the sub-line it stands for) or a structured row
        # that already carries one forward from an earlier save
        # (`reclaimed_line`, set by the bulk_create below). Remembered here,
        # before the delete destroys the row, so whichever cleaned set replaces
        # it can carry the SAME link on to the next save instead of losing it —
        # otherwise a second "Log session" before the athlete gets around to
        # retyping the sub-line would sever the link `_upsert_parsed_set` needs
        # to reuse the row instead of minting a twin.
        carried_links = []
        # Hidden is computed over the WHOLE log (#561), not filtered-then-per-row:
        # a copy left behind by an earlier "Log session" only answers to its line
        # through `reclaimed_line`, and the one-row-per-line ranking that decides
        # whether it's the one showing needs every row that could be displayed by
        # that same line — a query already scoped to this session's trainable
        # cells can't see them all.
        rows = list(log.sets.select_related("source_line", "reclaimed_line"))
        # #567/#568 P1-B: the PRE-DELETE snapshot of every pk this log holds
        # right now, before anything below deletes or renumbers a row. An id
        # naming one of these pks is "anchored" — real evidence about a row
        # this save is looking at, even the row it's about to replace,
        # because the payload that names it is exactly the payload doing the
        # replacing. An id naming no pk here is "stale": it belonged to a row
        # that is ALREADY gone (a write-ahead body replaying after its first
        # delivery already committed and moved on, say), and a stale id
        # carries no information at all — see `_client_held` and
        # `_consume_carried_link`, which both fall back to today's positional
        # match for a stale id rather than treating it as "no match".
        #
        # #567/#568 P1-G: a MAPPING (pk -> prescription_id), not a bare set of
        # pks. "Anchored" used to mean "this id names a live pk" alone, with
        # the prescription agreement checked separately at each of the three
        # call sites — so an id that named a real, live row under the WRONG
        # prescription (a crafted payload, or a genuine bug) was classified
        # anchored, failed that separate check, and every site simply
        # `continue`d without ever trying the positional fallback — degrading
        # to "no match at all" instead of "stale id, try position", exactly
        # the failure the stale-id rule exists to avoid. `_names_live_row`
        # folds the agreement into the anchoring test itself so there is
        # EXACTLY ONE place that decides it: an id anchors only when it names
        # a live row AND that row's own prescription is the one the payload
        # claims.
        live_rows = {row.pk: row.prescription_id for row in rows}
        hidden_pks = hidden_parsed_set_pks(rows)
        # Materialized once so the anchor map built for `bulk_create` below
        # (#578 C1) can reuse this same read instead of re-querying
        # `trainable_cells()` a second time.
        trainable_cells = list(session.trainable_cells())
        trainable_pks = {p.pk for p in trainable_cells}
        # #578 C1: `LoggedSet.exercise_slot_id` for a posted set, keyed by the
        # line-0 cell (`Prescription`) pk the payload names as
        # `prescription_id`. `_clean_logged_sets` validates against
        # `trainable_cells()` up front, at ~1520, BEFORE the lock this
        # function takes above — but `trainable_cells` here is read AGAIN
        # inside the transaction, and a coach's `prescription_skip` or
        # `prescription_delete` can commit in that gap and make a validated
        # cell non-trainable by the time we get here. So this map can be
        # missing a `cs["prescription_id"]` that was perfectly valid at
        # validation time — the `.get()` fallback below is not a "never
        # expected to fire" belt, it is a real, if narrow, race window.
        slot_id_by_cell_pk = {p.pk: p.exercise_slot_id for p in trainable_cells}
        # Close that gap directly: fetch the slot for any posted cell this
        # in-transaction read no longer counts as trainable, straight from
        # the cell itself rather than from `trainable_cells()`. A cell that
        # stopped being trainable didn't stop existing, and its
        # `exercise_slot_id` doesn't change just because it was skipped or
        # deleted — that's the whole anchor invariant this PR exists to
        # guarantee. Guarded by `if missing` so the ordinary (no race) path
        # costs no extra query.
        posted_prescription_ids = {cs["prescription_id"] for cs in cleaned_sets}
        missing = posted_prescription_ids - slot_id_by_cell_pk.keys()
        if missing:
            # Unscoped on purpose — do NOT filter this by `week=`/
            # `exercise_slot__session_slot=`. The cell's own `exercise_slot_id`
            # is the right answer regardless of which day its slot sits on
            # *right now*; that is the whole point of anchoring to the slot
            # instead of the cell. `_clean_logged_sets`, called at ~1520
            # (above this block), has already restricted every posted
            # `prescription_id` to this session's `trainable_cells()` — so
            # scoping this fallback query too doesn't add safety, it
            # reintroduces the bug this map exists to close: a coach's
            # `prescription_move` can commit between that validation and this
            # in-transaction read and re-home the slot onto another day's
            # `session_slot` (via a plain `ExerciseSlot.objects.filter(...)
            # .update(...)`, no lock shared with this session), which makes a
            # day/week-scoped query return nothing for a cell that is still
            # exactly the right cell. A zero-row result here leaves the
            # `.get()` fallback below to write `exercise_slot_id=None` — the
            # very NULL anchor this fallback was added to prevent — and
            # nothing ever repairs it afterward.
            slot_id_by_cell_pk.update(
                Prescription.objects.filter(pk__in=missing).values_list(
                    "pk", "exercise_slot_id"
                )
            )
        for row in rows:
            if row.prescription_id not in trainable_pks:
                continue
            if row.pk in hidden_pks:
                continue
            # #570: a row the logger cannot RENDER is one the client cannot
            # repost, so it is history rather than draft state — the same
            # reasoning the two skips above make. `presenters._set_rows` now
            # stops at MAX_LOGGED_SET_NUMBER, so a row left above it by the old
            # unbounded walk (or by `_upsert_parsed_set`'s own walk before it
            # was bounded) is absent from every payload, and without this skip
            # the replace below would delete it silently and nothing would
            # bring it back. Before the cap such a row rendered, the client
            # posted it, and the save 400'd with the row intact — a lockout,
            # which is bad, but not a silent deletion of a set the athlete
            # performed.
            #
            # Sparing it is not the same as repairing it. A row past the
            # ceiling whose `source_line` still names a live sub-line USUALLY
            # comes back into range the next time that line is edited —
            # `_upsert_parsed_set` re-picks its number from the bottom — but
            # not always: not when the legal range is already full (it then
            # keeps the number it just freed, deliberately, rather than be
            # deleted), and not when the edit lands on one of that function's
            # `existing` reuse branches, which keep the row and its number as
            # they are. A SOURCE-LESS row (a `reclaimed_line` copy) has no
            # such path at all — the renumbering loop below can never see it
            # either, since its slot can never appear in `posted`, which
            # `_clean_logged_sets` bounds to the legal range. Such a row stays
            # where it is, invisible on the page and still counting toward 1RM
            # and PRs. This skip preserves that state rather than fixing it,
            # which is the right way round: the alternative is deleting a set
            # the athlete performed.
            if row.set_number > MAX_LOGGED_SET_NUMBER:
                continue
            if row.source_line_id is not None and not _client_held(
                row, cleaned_sets, identified, live_rows
            ):
                continue
            replaceable.append(row.pk)
            if row.source_line_id is not None:
                link_id = row.source_line_id
            else:
                link_id = row.reclaimed_line_id
            if link_id is not None:
                carried_links.append(
                    {
                        "prescription_id": row.prescription_id,
                        "set_number": row.set_number,
                        "values": (row.reps, row.load, row.rpe),
                        "link_id": link_id,
                        # #567: lets a cleaned set in IDENTIFIED mode claim
                        # this link by the row's own pk rather than by the
                        # slot it used to occupy — the slot is exactly what a
                        # renumbering elsewhere in this same save can move.
                        "row_pk": row.pk,
                    }
                )
        log.sets.filter(pk__in=replaceable).delete()

        # Drop any posted row that merely re-states a surviving parsed set. The
        # payload is a snapshot of what the page rendered, and a reclaim can be
        # undone: a row the client saw (and so posted) can be hidden again by the
        # time the save lands, in which case it is NOT replaced above — and
        # creating it would leave the same performance twice in one log, once as
        # the parsed row and once as a source-less clone of it.
        #
        # Keyed on SET NUMBER as well as value, which is what separates "the
        # client is re-posting THIS row" from "the client is posting a set that
        # happens to look like a different one". Two identical performances on
        # two sub-lines are an ordinary thing to do — 225 x 5 twice — and
        # matching by value alone let the untouched survivor absorb the
        # replacement for the row just deleted, so that performance vanished.
        # The client reports the number ``serialize_session_log`` gave it, so
        # the row it means still carries that number here (the renumbering
        # below runs after this).
        #
        # Also keeps a surviving row that is hidden through `reclaimed_line`
        # (#561): after a coach undo the copy is hidden, so the replace above
        # spares it, but a page loaded BEFORE the undo still shows it as a
        # filled Set row and re-posts it — and letting that repost fall through
        # to the create below would log one performance twice. Recomputed on
        # the POST-DELETE rows on purpose: deleting the parsed row that used to
        # outrank a copy is exactly what makes the copy the row a line is
        # showing, so "hidden" can only be judged after the delete above runs.
        surviving = list(log.sets.select_related("source_line", "reclaimed_line"))
        hidden_pks = hidden_parsed_set_pks(surviving)
        available = [
            row
            for row in surviving
            if row.source_line_id is not None or row.pk in hidden_pks
        ]
        keep = []
        for cs in cleaned_sets:
            # #567: IDENTIFIED matches by the row's own pk, not the slot it
            # posted at. A hidden survivor's ``set_number`` can already have
            # moved (this is exactly the renumbering below, run by an earlier
            # save) while its pk hasn't — so pk is the only thing a stale
            # repost can still name correctly. The value check stays: an id
            # match with DIFFERENT values is an EDIT of a row the client can
            # no longer replace directly (it's hidden, and was spared above),
            # so it must fall through to the create below and let the
            # renumbering move the hidden row aside — the same "a visible
            # duplicate beats a silent deletion" call the rest of this slice
            # makes. A cleaned set that only carries a ``client_id`` (a grid
            # row with no server row) can never absorb anything here: its
            # ``id`` is ``None``, which no real row's pk equals, so the set
            # this issue used to swallow now always falls through to create.
            #
            # #567/#568 P1-B: the pk match above only applies to an ANCHORED
            # id (``_names_live_row`` — it names a pk this log held before
            # this save's delete, ``live_rows``). A STALE id names a row this
            # log no longer holds at all (a write-ahead replay whose first
            # delivery already replaced that row under a new pk, say) and so
            # carries no identity information — it degrades to the exact
            # positional test the id-less path below already uses, which is
            # what lets the replayed body absorb into the row its own earlier
            # delivery created, instead of creating a second copy of the same
            # performance (P1-B's "twin" scenario). A ``client_id`` (P1-B's
            # third case) never reaches a pk match at all — ``cs["id"]`` is
            # ``None`` — so it always falls through to the positional test
            # too; the positional test then requires the SAME slot+values,
            # which #567 B's own cleaned set never restates (it's a genuinely
            # new performance), so it still correctly finds no twin.
            #
            # #567/#568 P2-A/P1-G: an anchored match also requires the SAME
            # prescription as the row it names — a crafted payload can post a
            # real pk under the WRONG prescription, and pk equality alone
            # would let it absorb (or, via ``_client_held``/
            # ``_consume_carried_link``, spare or re-link) a row that belongs
            # to a different lift entirely. That agreement is now folded into
            # ``_names_live_row`` itself (P1-G) — checking it again here would
            # be redundant (once ``row.pk == cs["id"]`` and the id is
            # anchored, ``live_rows`` already guarantees the prescriptions
            # match) and this file no longer does, so there is exactly one
            # place that decides it.
            if identified and cs["id"] is not None and _names_live_row(cs, live_rows):
                twin = next(
                    (
                        row
                        for row in available
                        if row.pk == cs["id"]
                        and parsing.same_logged_set(
                            (row.reps, row.load, row.rpe),
                            (cs["reps"], cs["load"], cs["rpe"]),
                        )
                    ),
                    None,
                )
            elif identified and cs["id"] is not None:
                # STALE id (P1-B): no live pk to trust, so fall back to the
                # same positional test the id-less path below uses.
                twin = next(
                    (
                        row
                        for row in available
                        if (row.prescription_id, row.set_number)
                        == (cs["prescription_id"], cs["set_number"])
                        and parsing.same_logged_set(
                            (row.reps, row.load, row.rpe),
                            (cs["reps"], cs["load"], cs["rpe"]),
                        )
                    ),
                    None,
                )
            elif identified:
                # ``client_id`` (P1-B): names no server row by construction
                # (#567 B) — absorbs nothing, full stop.
                twin = None
            else:
                twin = next(
                    (
                        row
                        for row in available
                        if (row.prescription_id, row.set_number)
                        == (cs["prescription_id"], cs["set_number"])
                        and parsing.same_logged_set(
                            (row.reps, row.load, row.rpe),
                            (cs["reps"], cs["load"], cs["rpe"]),
                        )
                    ),
                    None,
                )
            if twin is not None:
                available.remove(twin)  # one survivor absorbs one posted row
                continue
            keep.append(cs)
        cleaned_sets = keep
        posted = {(cs["prescription_id"], cs["set_number"]) for cs in cleaned_sets}

        # Move any surviving PARSED row off a set number the client just posted.
        # Two rows sharing (prescription, set_number) collapse in
        # `athlete_session`'s dict, after which a save can delete both while
        # reposting one. The client's numbering stays authoritative; the parsed
        # row yields, because the client is the one with a page to keep in step.
        #
        # Covers the visible rows too, not just the hidden ones. A parsed row is
        # numbered by its sub-line while the structured grid numbers from 1, so
        # an athlete typing into structured row 1 collides with a parsed row on
        # sub-line 1 — and since such a row is now SPARED rather than deleted
        # (see `_client_held`), sparing it without renumbering simply moved the
        # collision one step later.
        #
        # Also covers a HIDDEN copy (#561, `display_line_id`): it keeps the set
        # number the parsed row had, so the athlete's now-empty Set row 1
        # collides with it too. The collision only bites later — a second
        # reclaim makes both rows visible, and one save can then delete both
        # while reposting one — which is the same hazard this loop already
        # exists for.
        if posted:
            for row in log.sets.select_related("source_line", "reclaimed_line"):
                if display_line_id(row) is None:
                    continue
                if (row.prescription_id, row.set_number) not in posted:
                    continue
                taken = set(
                    log.sets.filter(prescription_id=row.prescription_id)
                    .exclude(pk=row.pk)
                    .values_list("set_number", flat=True)
                ) | {n for (pid, n) in posted if pid == row.prescription_id}
                # #570: bounded at MAX_LOGGED_SET_NUMBER — the walk used to be
                # a plain `number += 1` with no ceiling, so a survivor could
                # climb past the number `_clean_logged_sets` will accept. The
                # presenter still rendered it as an ordinary fillable row, and
                # the moment the athlete filled or ticked it the endpoint
                # rejected the number and 400'd the WHOLE payload — a hard
                # lockout, with no way to save the session at all until
                # something moved the row back down.
                #
                # When nothing is free in the whole legal range the save is
                # REFUSED, rather than leaving this row on a number another row
                # already holds: the collision is exactly the two-rows-one-number
                # hazard this renumbering exists to prevent, and reinstating it
                # here to avoid an awkward answer would let one later save delete
                # both rows while reposting one.
                #
                # `set_rollback` before the return, and it is load-bearing:
                # `log.sets.filter(pk__in=replaceable).delete()` has already run
                # in this same block, so returning a response without it would
                # COMMIT those deletes and refuse the save anyway — the athlete's
                # rows gone AND the save rejected. Marked for rollback, this
                # block's exit rolls everything back, so a refused save writes
                # nothing at all. Nothing else in the block runs after this
                # return, so no query can hit the poisoned transaction.
                number = _first_free_set_number(taken, row.set_number)
                if number is None:
                    # Name the exercise BEFORE marking the rollback: once the
                    # transaction is poisoned no query may run, and
                    # `row.prescription` is a lazy FK fetch.
                    refused = f"Too many sets logged for {row.prescription.name}."
                    # 400, not `athlete_cell_write`'s 503 (#571) — deliberately
                    # the other way, and both are load-bearing: this refusal is
                    # DETERMINISTIC (the same payload exhausts the same range
                    # again), so retrying it can only fail again and the client
                    # drops the queued entry rather than keep retrying
                    # something that can never succeed. #571's 503 is for a
                    # write that MIGHT yet land — a poisoned connection, not a
                    # rejected payload — so that client keeps its entry queued
                    # and retries.
                    transaction.set_rollback(True)
                    # JSON with an `error` string, not the bare
                    # `HttpResponseBadRequest` this endpoint's OTHER 400s use,
                    # and the difference is the contract: `meso_athlete.js`
                    # renders `error` to the athlete VERBATIM, so only a
                    # refusal actually written for them may carry that shape.
                    # The validation 400s from `_clean_logged_sets`
                    # ("Duplicate id in sets.") are developer-facing and stay
                    # plain text, which is exactly how the client tells the
                    # two apart instead of guessing from the body.
                    return JsonResponse({"ok": False, "error": refused}, status=400)
                row.set_number = number
                row.save(update_fields=["set_number"])

        # #541: a cleaned set inherits the link `carried_links` remembered for
        # the row it's replacing ONLY when it restates that row verbatim — same
        # slot AND the same values `_client_held` uses to decide a payload
        # actually reposts a given row. An edit (a different value on the same
        # slot) is a different performance and gets no link, which is exactly
        # how the carry is meant to end: the moment the athlete or coach
        # changes the numbers instead of retyping them back, there is no
        # "restore" left to reuse the row for. Each carried link is consumed at
        # most once so two cleaned sets can never both claim it.
        created_rows = LoggedSet.objects.bulk_create(
            [
                LoggedSet(
                    session_log=log,
                    prescription_id=cs["prescription_id"],
                    # #578 C1: written alongside `prescription_id`, not instead
                    # of it — a code rollback mid-deploy must leave the old
                    # `prescription`-reading derivations working. See
                    # `LoggedSet.exercise_slot`'s model comment.
                    exercise_slot_id=slot_id_by_cell_pk.get(cs["prescription_id"]),
                    set_number=cs["set_number"],
                    reps=cs["reps"],
                    load=cs["load"],
                    rpe=cs["rpe"],
                    reclaimed_line_id=_consume_carried_link(
                        carried_links, cs, identified, live_rows
                    ),
                )
                for cs in cleaned_sets
            ]
        )
        # #567: hands the client back the server pk for every row it minted
        # itself this save (a ``client_id``, no ``id``), so a page that just
        # created a grid row learns the id it must post from now on — without
        # this, that row would stay ``client_id``-only forever and never
        # qualify for the identified match above. Zipped positionally: the
        # cleaned sets fed `bulk_create` (in this list-comprehension order) are
        # positionally exactly this save's `created_rows`, one per element,
        # since `bulk_create` neither reorders nor drops fed rows. Skips an
        # entry with no `client_id` (nothing to report) or no returned `pk`
        # (a backend that doesn't hand pks back from `bulk_create` — the
        # response is then simply silent about this row's id, exactly like
        # any other read that follows such an insert).
        client_ids = {
            row.pk: cs["client_id"]
            for row, cs in zip(created_rows, cleaned_sets)
            if cs["client_id"] is not None and row.pk is not None
        }
        # Net growth, not "a row was written" (#509 set_logged): this save
        # REPLACES rows (delete + bulk_create), so row identity doesn't survive
        # it — a resave, an edit, or reposting sets the typed path already
        # counted all replace-then-recreate their own rows and must add zero.
        # Known undercount: removing one set and adding another in the same
        # save nets zero.
        new_sets = max(0, log.sets.count() - sets_before)
        # Refresh the athlete's persisted 1RM for this session's lifts from their
        # *completed* logs. Run on every save, not only a done one: a save that
        # edits an already-DONE log's sets (a heavier set, a correction, a
        # removed basis) changes exactly the history derivation reads from, so
        # skipping the refresh on a "pending" save would leave a stale estimate
        # until some later done save happened to fix it. (Status can no longer
        # move DONE->PENDING here at all — see the sticky-status comment above —
        # so this is never clearing an estimate a downgrade just orphaned; it is
        # only ever keeping a DONE log's own estimate current.) Recomputes from
        # scratch — a heavier set raises it, an edit that drops the PR lowers
        # it, a removed basis clears it.
        meso_one_rm.refresh_one_rms(
            request.user,
            list(session.trainable_cells()),
            session.week.mesocycle.plan.unit,
        )
    for _ in range(new_sets):
        track(EventName.SET_LOGGED, actor=request.user, subject=log, via="log")
    if not was_done and log.status == SessionLog.Status.DONE:
        track(EventName.SESSION_COMPLETED, actor=request.user, subject=log, via="log")
    # #441 P3-5: the results step auto-advances once the coach *completes* one of
    # their own self-link sessions. Gated on the step's own predicate so a
    # ``pending`` "save progress" — or a done log the coach makes as an athlete
    # under *another* coach — never skips the step. A no-op unless parked on
    # results.
    meso_tour.advance_self_step_if_complete(request.user, "results")
    # Phase 4c: the lifts in this session that beat the athlete's prior best, so
    # the logger can celebrate a PR the instant it's logged. Pure detection off
    # the just-committed rows. As of 5a this read is LIVE (it counts pending
    # sets), so a "Save progress" draft can legitimately return records too —
    # it is no longer DONE-gated.
    #
    # Minus anything this save did not actually log. A parsed row that is still
    # displayed by its own sub-line was celebrated by the blur that created it
    # (``_upsert_parsed_set`` fires the optimistic toast), and it survives this
    # save untouched — so reporting it here congratulated the athlete a second
    # time for a record they had already seen, on a save that changed nothing.
    hidden_set_pks = hidden_parsed_set_pks(
        log.sets.select_related("source_line", "reclaimed_line")
    )
    new_records = [
        r for r in new_records_in(log) if r.logged_set_id not in hidden_set_pks
    ]
    return JsonResponse(
        {
            "ok": True,
            "log": serialize_session_log(log, client_ids=client_ids),
            "new_records": [serialize_new_record(r) for r in new_records],
        }
    )


@login_required
@require_POST
def athlete_set_one_rm(request, pk):
    """Set or clear the athlete's *manual* 1RM for a lift in a session they own.

    The estimated 1RM was per-device localStorage (Phase 2b); this persists the
    athlete's typed value server-side as a ``source=manual`` ``AthleteOneRm`` so
    it syncs across devices and is visible to the coach. The body is
    ``{"prescription": <id>, "value": "140"}`` — a blank/absent ``value`` *clears*
    it back to the log-derived estimate. Scoped exactly like the log endpoint
    (``_athlete_session_or_404``): the prescription must live in a
    session the athlete owns, else a flat 404/400 — never a write to a foreign
    lift. A manual value overrides the log-derived estimate and survives later
    logs.
    """
    session = _athlete_session_or_404(request.user, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    presc_id = payload.get("prescription")
    prescriptions = {p.pk: p for p in session.trainable_cells()}
    # ``bool`` is an ``int`` subclass — reject it explicitly so ``true`` isn't an id.
    if (
        not isinstance(presc_id, int)
        or isinstance(presc_id, bool)
        or presc_id not in prescriptions
    ):
        return HttpResponseBadRequest("prescription must be one of this session's.")

    value, ok = meso_one_rm.clean_manual_value(payload.get("value"))
    if not ok:
        return HttpResponseBadRequest("value must be a positive number or blank.")

    row = meso_one_rm.set_manual_one_rm(
        request.user,
        prescriptions[presc_id],
        value,
        session.week.mesocycle.plan.unit,
    )
    return JsonResponse(
        {
            "ok": True,
            "one_rm": presenters._one_rm_label(row),
            "source": row.source if row is not None else "",
        }
    )


@login_required
@require_POST
def athlete_cell_write(request, pk):
    """Upsert one freeform sub-line cell the athlete authored (Phase 4a).

    The athlete's editable tracking stack beneath each exercise: body
    ``{"exercise_id": <int>, "line": <int>, "text": "<str>"}`` upserts the
    (exercise_slot × week × line) cell the coach's ``cell_line_write`` also
    addresses — but stamps it ``athlete_authored=True`` so it stays OUT of the
    coach's undo/redo snapshot machinery (a coach undo must never revert or
    hard-delete an athlete's note; see ``history.py``).

    Mirrors ``athlete_log_session``'s discipline: athlete-scoped by
    ``_athlete_session_or_404`` (foreign/archived/unknown → flat 404), NO
    billing gate (the coach's over-limit freeze doesn't touch the athlete's own
    tracking), the body fully validated before any write (a bad request is a
    400 that persists nothing), and the write is an idempotent upsert. It
    records NO ``PlanAction``. ``line`` 0 is rejected (that's the coach's
    prescription line), as is ``line`` > ``MAX_CELL_LINE``; blank text clears
    the sub-line in place.
    """
    # A write queued offline by another account (#527). localStorage outlasts
    # a logout, so a page left open for athlete A can flush A's queued lines
    # after athlete B signs in on another tab — under B's session cookie. The
    # scoping below would 404 them, and the client drops a refused line. Say
    # "wrong account" instead, which the client keeps queued for its owner.
    # Decided from the body alone, so it says nothing about the session.
    if _sent_by_another_account(request):
        return JsonResponse(
            {"ok": False, "error": "This write belongs to another account."},
            status=409,
        )
    session = _athlete_session_or_404(request.user, pk)
    plan = session.week.mesocycle.plan
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    # ``bool`` is an ``int`` subclass — reject it so ``true`` isn't an id/line.
    exercise_id = payload.get("exercise_id")
    if not isinstance(exercise_id, int) or isinstance(exercise_id, bool):
        return HttpResponseBadRequest("exercise_id must be an integer.")
    # The exercise's line-0 cell must live in THIS session (parity with
    # ``athlete_set_one_rm``): a foreign id — even a valid one — is a 400.
    line_zero = {p.pk: p for p in session.cells()}
    if exercise_id not in line_zero:
        return HttpResponseBadRequest("exercise_id must be one of this session's.")
    slot = line_zero[exercise_id].exercise_slot

    line = payload.get("line")
    if (
        not isinstance(line, int)
        or isinstance(line, bool)
        or not 1 <= line <= MAX_CELL_LINE
    ):
        return HttpResponseBadRequest(
            f"line must be an integer between 1 and {MAX_CELL_LINE}."
        )

    text = payload.get("text")
    if not isinstance(text, str):
        return HttpResponseBadRequest("text must be a string.")
    if len(text) > PATCHABLE_FIELDS["text"]:
        return HttpResponseBadRequest("text is too long.")

    # #571: whether the connection came out of the block below poisoned — a
    # savepoint's OWN rollback failed (dropped connection, a pgbouncer
    # `server_lifetime` cycle, a mid-request DB restart), which is the one
    # way `_upsert_parsed_set`'s tolerance guarantee (its docstring's "a DB
    # failure here rolls back only the upsert") stops holding. Read INSIDE
    # the `with` block, never after: `Atomic.__exit__` clears
    # `connection.needs_rollback` on its own way out, so a check placed after
    # the block always reads a freshly-cleared flag and never catches this.
    poisoned = False
    with transaction.atomic():
        # LOCK ORDER (#562) — the Plan row FIRST, above the Session lock below.
        # The app-wide order is written down in ``docs/meso/decisions.md``
        # ("Row-lock order"): Plan, then Session, then their children.
        #
        # This path already took both of those locks; it took them in the
        # wrong order. `_touch_plan` further down UPDATEs the Plan row — an
        # implicit exclusive row lock — so a blur that changed anything held
        # Session-then-Plan, while `api_plan_undo`/`api_plan_redo` hold Plan
        # (`select_for_update`) and then UPDATE every snapshotted Session row
        # from inside `restore_plan_snapshot`. An athlete blur overlapping a
        # coach undo on the same plan closed that cycle, and Postgres aborted
        # one side: either the athlete's set or the coach's undo 500'd.
        #
        # `no_key=True` deliberately. FOR NO KEY UPDATE is the exact strength
        # `_touch_plan`'s own UPDATE takes, so this adds no strength the path
        # didn't already need; it still conflicts with the undo/redo
        # endpoints' FOR NO KEY UPDATE, which is what makes the two genuinely
        # exclude each other; and it does NOT conflict with the FOR KEY SHARE
        # a concurrent insert of some other Plan child takes on its deferred
        # FK at commit time — the trap #560 hit on a user row.
        #
        # Taken UNCONDITIONALLY, including on the `untouched_coach_line` no-op
        # path that never reaches `_touch_plan`. Deciding whether to lock would
        # mean reading the cell first, and the Session lock has to sit above
        # that read (next comment) — so the real choice is a Plan lock on
        # every blur, or Session-before-Plan on some of them, and the order is
        # what matters.
        #
        # Be honest about the cost, which is latency on EVERY blur and not just
        # on the ones that write: the template posts here on every focus change,
        # most of those change nothing, and each now waits for any coach
        # transaction holding this plan's row — including a designer save, whose
        # Plan lock spans `serialize_plan_snapshot`'s whole-plan JSON build
        # (`record_plan_action`). Lines replayed from the offline write-ahead
        # queue (#529) queue behind the same lock. Accepted: a plan is one
        # athlete's, so the wait is against their own coach editing that very
        # plan, and the alternative is a deadlock that 500s one of them.
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        # Serialize the WHOLE write on the session row, before anything is read.
        #
        # `(session, athlete)` has no uniqueness, so two overlapping blurs can
        # each see no SessionLog and create one, splitting a workout across two
        # logs — every later read takes only the newest, so the sets stranded on
        # the older one vanish from DONE coach results and the 1RM refresh.
        #
        # The lock has to sit ABOVE the `previous_text` read, not inside the
        # upsert: two writes for the same sub-line (two tabs, an offline retry)
        # could otherwise both read the old text, and the later one would judge
        # the row the earlier one just created against stale text, find nothing
        # of "its own" to replace, and append a duplicate. The client-side
        # promise chain orders one page's saves; only this orders the server.
        #
        # A no-op on SQLite (which serializes writers anyway), real on Postgres.
        Session.objects.select_for_update(of=("self",)).filter(pk=session.pk).first()
        cell, created_cell = Prescription.objects.get_or_create(
            exercise_slot=slot, week=session.week, line=line
        )
        # The template posts on EVERY blur, so most requests carry text nobody
        # touched — including the coach's own cues, which the athlete can focus
        # and leave. Claiming authorship of those was doing real damage:
        #
        #   * a cue that happens to read like a set (`225 x 5`) became
        #     athlete-authored and parsed into a pending LoggedSet and a PR for
        #     a performance the athlete never did;
        #   * a RECLAIMED line's set went back into hiding, because
        #     `HIDDEN_PARSED_SET` keys on this flag — invisible everywhere while
        #     still counting, and the delete below would then destroy it.
        #
        # Authorship follows actual authorship: a blur that changes nothing on a
        # line the athlete doesn't own is a no-op, full stop. A genuine edit
        # still claims the line (and re-parses it) as before.
        # What this line was DISPLAYING before this write. The upsert needs it to
        # tell its own rows — the ones this line was showing — from history
        # handed to the structured logger by a reclaim.
        previous_text = "" if created_cell else cell.text
        untouched_coach_line = (
            not created_cell and not cell.athlete_authored and cell.text == text
        )
        if not untouched_coach_line:
            cell.text = text
            cell.athlete_authored = True
            cell.save(update_fields=["text", "athlete_authored"])
            # LOCK ORDER — load-bearing, and since #562 satisfied STRUCTURALLY:
            # the Plan row is already held from the top of this block, so this
            # call is only the `modified` bump, not the acquisition.
            #
            # The requirement it used to carry on its own: `_upsert_parsed_set`
            # below locks the line-0 Prescription (its re-read is
            # `select_for_update`), and Plan MUST be held first, because
            # `prescription_skip` locks Plan (`record_plan_action`,
            # history.py) then that same Prescription (`cell.save`). A coach
            # skip racing an athlete blur on the same row deadlocks on Postgres
            # if the two disagree on order. (This is exactly what a review
            # round got wrong: `prescription_skip` is Plan→Prescription, not
            # the reverse.) That still holds — it is simply no longer this
            # line's job to establish it, and moving the Prescription lock
            # ahead of the Plan lock at the top of the block is what would now
            # break it.
            _touch_plan(plan)
        # Parse-at-commit (5a): derive a silent, structured LoggedSet from the
        # text just committed above. Defensively wrapped inside the helper — a
        # parse/upsert problem is logged and swallowed, never surfaced here.
        # The return is the optimistic-PR-toast payload (§7) — empty on any
        # failure, never raises.
        # The sets this line derives BEFORE the upsert, for the activity bump
        # below (5b) — a set can change while the text doesn't.
        sets_before = (
            None if untouched_coach_line else _line_sets(session, request.user, cell)
        )
        new_records = (
            []
            if untouched_coach_line
            else _upsert_parsed_set(
                session,
                request.user,
                line_zero[exercise_id],
                cell,
                previous_text=previous_text,
            )
        )
        # Bump `last_activity_at` (5b, settle.py) — but ONLY on a real edit.
        # The template posts on EVERY blur, so most requests change nothing,
        # and bumping on those would keep an abandoned session from ever going
        # quiet long enough to settle. A real edit is either new text, or the
        # same text now deriving different sets: a line typed while its row was
        # skipped saves no set, and once the coach un-skips the row, re-blurring
        # that unchanged text creates one. Values are compared, not pks — the
        # upsert recreates the row even on an unchanged re-blur.
        #
        # A queryset UPDATE, not `log.save()`: there may be no log at all (a
        # note blur that never wanted a set). It runs outside
        # `_upsert_parsed_set`'s savepoint, so a swallowed upsert failure can't
        # roll it back. Status doesn't matter — the sweep only reads the field
        # on PENDING logs.
        #
        # #571 GUARD (shape 1): `not connection.needs_rollback` sits FIRST in
        # the `and` chain, ahead of everything else, so a poisoned connection
        # short-circuits the whole condition before either `_line_sets(...)`
        # (a query) or the `.update()` below ever runs. Both are queries, and
        # Django refuses to run a query against a connection it has marked
        # for rollback — it raises `TransactionManagementError` instead,
        # which would escape this view as a 500 AND take the outer atomic's
        # already-committed `cell.save()` down with it. Skipping the activity
        # bump here costs nothing but best-effort telemetry; whether the
        # response can still honestly say "ok" is decided by the CAPTURE
        # below, once the block is done.
        if (
            not connection.needs_rollback
            and not untouched_coach_line
            and (
                text != previous_text
                or _line_sets(session, request.user, cell) != sets_before
            )
        ):
            SessionLog.objects.filter(session=session, athlete=request.user).update(
                last_activity_at=timezone.now()
            )
        # #571 CAPTURE (shape 2): the LAST statement in the block, deliberately
        # — a plain attribute read, not a query, so it's safe to run even
        # against a poisoned connection. `Atomic.__exit__` clears
        # `connection.needs_rollback` on its own way out (that's what lets a
        # savepoint's failed rollback be absorbed one level up in the first
        # place), so a check placed AFTER the `with` block always reads an
        # already-cleared flag and would never see this. If the connection is
        # genuinely dead rather than merely marked, the block's own exit can
        # raise here instead — a 500, which is honest in the same direction:
        # the client treats any 5xx as "kept, not lost" (see the return
        # below) and retries.
        poisoned = connection.needs_rollback
    if poisoned:
        # #571: never claim a save the database didn't keep. `needs_rollback`
        # surviving to here means the block above rolled back SILENTLY on
        # exit, with nothing raised to say so — `cell.save()` and any parsed
        # `LoggedSet` are gone (the issue's shape 2), so answering `ok: True`
        # would tell the athlete a write happened that the database doesn't
        # have.
        #
        # 503, not a 4xx. `meso_athlete.js`'s `isRetryableStatus` treats
        # `status >= 500` (plus 408/429) as outcome `"kept"` — the server
        # failed, not the write — and leaves the line's outbox entry in
        # place with `entry.savedText` cleared, so the next blur reposts it.
        # A 4xx reads as `"rejected"`: dropped from the outbox and never
        # retried, which is exactly wrong for a write the database never
        # actually kept. Do not "tidy" this into a 400 later.
        return JsonResponse(
            {"ok": False, "error": "Could not confirm the save. Please try again."},
            status=503,
        )
    # Read once, reported twice — the tint and the reason behind it have to be
    # the SAME answer, and deriving them separately would mean two reads of a
    # moment that can move between them.
    warn_reason = _cell_warn_reason_or_blank(
        cell, line_zero[exercise_id], session=session, athlete=request.user
    )
    return JsonResponse(
        {
            "ok": True,
            "cell": {
                "id": cell.pk,
                "exercise_slot_id": slot.pk,
                "week_id": session.week_id,
                "line": cell.line,
                "text": cell.text,
                # Derive-on-read warn (5a, plan §8) — re-classified from the
                # just-committed text so a re-blur that fixes a fat-fingered
                # set attempt clears the warning without a page reload.
                # `loggable` carries the one reason the TEXT can't reveal: a
                # skipped row accepts no sets, so set-shaped text on a stale
                # page is saved but never logged, and saying nothing would let
                # the athlete believe it counted.
                # Same rule the presenter applies, so the tint can't clear
                # here only to come back on reload. `loggable` carries the one
                # reason the text can't reveal: a skipped row accepts no sets.
                #
                # #567/#568 P1-D: computed HERE, OUTSIDE the atomic block
                # above — not inside it, and not under a savepoint of its own.
                # An earlier version of the #568 fix did both, reasoning by
                # analogy with `_upsert_parsed_set`'s own "load-bearing, not
                # decorative" nested atomic; that reasoning does not transfer
                # to this read, and doing it anyway was actively dangerous.
                # See `_cell_warn_reason_or_blank`'s own docstring for why.
                "warn": bool(warn_reason),
                # ...and WHY it's tinted (#572). The client re-posts a warned
                # line whose text hasn't changed — the repair for set-shaped
                # text typed while the row was skipped — and that same rule,
                # fired for a line tinted only because its set is on the day
                # the coach moved this exercise FROM, minted a SECOND
                # LoggedSet for one performance. A bare boolean cannot tell
                # the two apart, so the reason rides alongside it; see
                # `sub_line_warn_reason`.
                "warn_reason": warn_reason,
            },
            # Optimistic PR toast (5a, plan §7): any lift this parsed set just
            # beat the athlete's current LIVE best on — mirrors
            # athlete_log_session's wiring of new_records_in/
            # serialize_new_record, but off the *live* (PENDING-inclusive)
            # read, so it can fire before the session ever reaches DONE. Can
            # occasionally be a false alarm if the set is later corrected —
            # the accepted trade for in-the-moment feedback (5b settles it).
            "new_records": [serialize_new_record(r) for r in new_records],
        }
    )


def _sent_by_another_account(request):
    """Whether a cell write names an ``owner`` other than the signed-in user.

    The athlete logger stamps its queued writes with the account that typed
    them (``log_data.owner``). A write without one (an older page) is never
    treated as foreign.
    """
    try:
        owner = json.loads(request.body or "{}").get("owner")
    except (ValueError, AttributeError):
        return False
    return isinstance(owner, str) and owner != "" and owner != str(request.user.pk)


def _line_sets(session, athlete, cell):
    """Every ``LoggedSet`` ``cell`` derives for this athlete, as comparable values.

    ``athlete_cell_write`` snapshots this around the upsert to tell a blur that
    changed the athlete's logged data from one that didn't (5b's activity bump).
    """
    return sorted(
        LoggedSet.objects.filter(
            session_log__session=session,
            session_log__athlete=athlete,
            source_line=cell,
        ).values_list(
            "session_log_id", "prescription_id", "set_number", "reps", "load", "rpe"
        )
    )


def _upsert_parsed_set(session, athlete, line_zero_cell, cell, *, previous_text=""):
    """Parse ``cell``'s just-committed text and upsert its derivative ``LoggedSet``.

    Parse-at-commit (5a, docs/meso/parse-at-commit-plan.md §5): the freeform
    sub-line stays the athlete's source of truth; this derives a silent,
    machine-readable ``LoggedSet`` alongside it, scoped to ``(session_log,
    source_line=cell)`` so a re-blur deletes-then-recreates rather than
    appending. A blank/unparseable/skip/swap/note/duration cell just runs the
    delete — mirroring "blank clears the cell".

    A blur is a draft, not a completion: a newly created ``SessionLog`` is
    left at its default ``PENDING`` status, and an existing log's status is
    never touched here, so a DONE log is never downgraded.

    Returns the list of ``personal_records.NewRecord``s this upsert unlocks
    (§7) — computed off the same, now-PENDING-inclusive ``new_records_in``
    ``athlete_log_session`` already uses, so a blur can surface the same
    optimistic 🎉 the structured logger does, without waiting for DONE. Always
    ``[]`` when nothing beat the live best, or when the guard below caught an
    error.

    **Tolerance guard (non-negotiable):** ``cell.save`` already committed the
    raw text before this runs. The entire parse+upsert (including the
    new-records read) is wrapped so ANY unexpected error is logged and
    swallowed — the athlete's text is never lost and the response never turns
    into a 4xx/5xx over a parse problem. ``parse_performed`` is itself total
    (never raises), so this is belt-and-suspenders against everything else in
    the upsert (DB errors, future parser changes, etc).

    The inner ``transaction.atomic()`` is **load-bearing, not decorative**.
    This runs inside the caller's atomic block, and a *database* error marks
    the whole transaction ``needs_rollback`` — merely catching it would NOT
    save us: the outer block would still roll back on exit and take the
    already-committed ``cell.save`` with it, losing the athlete's text (the
    exact thing this guard exists to prevent). The nested block is a
    savepoint, so a DB failure here rolls back only the upsert and leaves the
    cell write intact.
    """
    new_records = []
    try:
        with transaction.atomic():  # savepoint — see the docstring
            # This guard runs BEFORE anything is read or written. Placed after
            # the log lookup it still prevented the set, but the log had already
            # been created — leaving the empty PENDING row that reads as
            # activity everywhere (see `_reap_empty_pending_log`).
            #
            # Re-read the line-0 cell UNDER A ROW LOCK, and do it BEFORE the
            # skip bail below. `line_zero` was built before the transaction, and
            # the session lock doesn't help — `prescription_skip` never touches
            # the session row. Locking the Prescription makes that UPDATE wait.
            #
            # Order matters as much as the lock: with the bail reading the stale
            # instance first, a skip landing mid-blur only turned `wants_set`
            # off, and execution still fell through to the delete — losing an
            # already-logged performance on an unchanged stale blur, which is
            # precisely the data loss the bail exists to prevent.
            fresh_line_zero = (
                Prescription.objects.select_for_update()
                .filter(pk=line_zero_cell.pk)
                .first()
            )
            if fresh_line_zero is None:
                # The row is gone (a history restore hard-deletes a stray cell).
                # Falling back to the stale instance wrote its dead pk as the
                # new set's FK; PostgreSQL defers that constraint to COMMIT, so
                # the violation surfaced in the OUTER transaction — past this
                # savepoint AND past the guard below — taking the athlete's
                # just-saved text down with it and returning a 500. There is no
                # prescription left to log against, and the text is already
                # saved either way.
                return []
            line_zero_cell = fresh_line_zero

            # A skipped row is READ-ONLY to this path, not just un-writable.
            # Declining to MINT a set isn't enough — letting the delete run
            # turned "coach skips a row" plus "the athlete's open page fires one
            # more blur" into data loss, contradicting `prescription_skip`,
            # which deliberately preserves work the athlete already did. Bail
            # before touching anything; the cell's text is saved either way.
            if line_zero_cell.skipped:
                return []

            parsed = parse_performed(cell.text)
            wants_set = bool(
                parsed
                and parsed.get("kind") == "set"
                and (parsed.get("reps") or parsed.get("load"))
            )

            # The shared newest-log rule — see models.newest_session_logs.
            log = newest_session_logs(session, athlete).first()
            if log is None:
                # Parse BEFORE creating. Creating up front meant a no-op blur —
                # tapping "add a line" and leaving it empty — persisted a dated
                # PENDING log with no sets, and `_scroll_hint`,
                # `_athlete_default_plan_id` and `serialize_recent_logs` all
                # read ANY SessionLog as activity. So an idle UI gesture moved
                # the athlete's last-trained week and polluted recent-log
                # grounding. With no log there is also nothing to delete, so
                # nothing to do at all.
                if not wants_set:
                    return []
                # Stamp the date like `athlete_log_session` does, even for a
                # pending draft. Left NULL, these logs sort BEFORE real dates
                # under Postgres's `-date` (NULLs first in DESC), so an old
                # parsed draft would pose as the newest log in recent-log
                # grounding, and record provenance would lose its workout date.
                log = SessionLog.objects.create(
                    session=session, athlete=athlete, date=timezone.localdate()
                )

            # Replace only the rows THIS LINE WAS SHOWING. A set the line no
            # longer displays was handed to the structured logger by a reclaim
            # and is now visible history — the athlete editing this line to a
            # note, a blank, or a different set must not erase a performance
            # they already earned. Judged against `previous_text`, not the text
            # just saved: under the NEW text a normal re-blur's own row looks
            # unrelated too, and sparing it would append instead of replace.
            #
            # Scoped to `line_zero_cell` as well as `source_line` (#570 round
            # 3). Every row this path creates carries BOTH — same slot, same
            # blur — so the prescription filter costs nothing in the ordinary
            # case, and it closes one that isn't ordinary: a row pointing at
            # this sub-line while belonging to a DIFFERENT exercise is
            # incoherent data, and deleting it here charged the athlete for
            # that. `taken` below is scoped to this prescription, so such a
            # row's freed number says nothing about where the replacement can
            # go — the delete could succeed while the create had nowhere to
            # land, which is a performed set destroyed outright. Sparing it
            # instead is the same call the replace-delete's own
            # trainable/hidden skips already make: a row this path cannot
            # account for is history, not draft state.
            mine = [
                row
                for row in log.sets.filter(
                    source_line=cell, prescription=line_zero_cell
                )
                if parsing.performed_text_shows(
                    previous_text, reps=row.reps, load=row.load, rpe=row.rpe
                )
            ]
            # The numbers those rows are about to free. Load-bearing for the
            # bounded walk below (#570 round 2): a row left ABOVE the ceiling
            # by the old unbounded walk frees a number outside the range
            # `_first_free_set_number` scans, so that helper answers "nothing
            # free" while this line's own slot is sitting right there — and
            # declining to create then DELETES a performed set and puts
            # nothing back, which is worse than the out-of-range row it was
            # trying to avoid. Captured before the delete, since afterwards
            # there is nothing left to ask.
            freed_numbers = sorted({row.set_number for row in mine})
            # What this cell held before, so an unchanged re-blur can be told
            # apart from a real edit (see the toast filter below).
            previous = mine[0] if mine else None
            previous_values = (
                (previous.reps, previous.load, previous.rpe) if previous else None
            )
            log.sets.filter(pk__in=[row.pk for row in mine]).delete()

            created = None
            unchanged = False
            # set_logged analytics (#509): only the CREATE branch below mints a
            # genuinely new set, and only when this line wasn't already
            # showing one of its own — an unchanged re-blur or a value edit
            # both delete-then-recreate an EXISTING performance, not a new one.
            is_new_set = False
            if wants_set:
                values = {
                    # NOT `parsed["reps"]` — a set's right-hand side lands in
                    # one of four keys, and reading only `reps` blanked every
                    # range (`225 x 5-8`), timed set (`225 x 30s`) and AMRAP,
                    # rendering them `— @ 225` in coach results.
                    "reps": performed_reps_text(parsed),
                    "load": str(parsed.get("load", "")),
                    "rpe": str(parsed.get("rpe", "")),
                }
                # Bound the fields the same way the structured logger does. A
                # parsed value longer than the column raises on Postgres, and
                # since we're inside the savepoint the guard would swallow it
                # and roll the DELETE back too — leaving the OLD set counting
                # while the response cheerfully reported warn=false.
                #
                # Same constant `cell_warn_reason` tests (`too-long`), deliberately:
                # storing nothing is fine, but the cell has to SAY so, and the
                # two would be free to drift if each had its own limit.
                if all(
                    len(value) <= parsing.MAX_LOGGED_FIELD for value in values.values()
                ):
                    # The sub-line's own position, NOT a constant 1. Every
                    # parsed row landing on set 1 was invisible while they
                    # stayed suppressed, but structured surfaces collapse by
                    # (prescription, set_number) — so once reclaim made them
                    # visible, two tracking lines showed and reposted as one
                    # set, and results labelled both "set 1".
                    #
                    # ...but the line's number can already be taken, by history
                    # this same line left behind: a reclaimed set is preserved,
                    # and a new set typed on that line would otherwise collide
                    # with it, and collapse the moment a second reclaim made
                    # both visible. Fall through to the next free number. The
                    # rows being replaced are already deleted above, so an
                    # ordinary re-blur finds its own number free and keeps it
                    # (idempotent).
                    # Restoring a reclaimed line to what it originally said is
                    # not a new performance. `mine` is empty in that case — the
                    # old row survived a reclaim, so `previous_text` (the coach's
                    # cue) no longer describes it — and creating would leave two
                    # identical rows on one source line, BOTH hidden by the
                    # restored text and both counted, overstating the workout
                    # with nothing on screen to show for it. Reuse the row.
                    #
                    # Scoped by ``prescription`` as well as ``source_line``
                    # (#577), matching the ``mine`` delete above and the
                    # ``reclaimed_line`` lookup below — unchanged by #578 C1.
                    # A row whose ``prescription`` went NULL (a purge
                    # hard-deleted its line-0 cell) still matches
                    # ``source_line=cell``, but this filter refuses to adopt
                    # it, same as before; the ``reclaimed_line`` lookup below
                    # filters ``prescription=line_zero_cell`` too, so it
                    # doesn't repair the row either — it falls through to the
                    # CREATE branch further down. What C1 changes is the
                    # consequence of that fall-through: the NULL-``prescription``
                    # row is no longer inert. Its own ``exercise_slot``
                    # (untouched by the ``Prescription`` delete) still counts
                    # it toward 1RM and PRs, so the fresh row the CREATE
                    # mints now counts *alongside* it — the same performance
                    # double-counted, where before the orphan was silently
                    # invisible and the fresh row was the only one that
                    # counted. That's a pre-existing data-integrity artifact
                    # of the hard-deleted cell (production has no such rows
                    # today), left as-is for #578's later stages: narrowing
                    # this filter to adopt the orphan, or widening it to
                    # dedupe against ``exercise_slot``, is a write-path
                    # decision that belongs to C2, not this comment fix.
                    existing = next(
                        (
                            row
                            for row in log.sets.filter(
                                source_line=cell, prescription=line_zero_cell
                            )
                            if parsing.same_logged_set(
                                (row.reps, row.load, row.rpe),
                                (values["reps"], values["load"], values["rpe"]),
                            )
                        ),
                        None,
                    )
                    # #541: a "Log session" between the reclaim and the restore
                    # replaced that row with a source-less structured copy that
                    # answers to this line through `reclaimed_line` instead, so
                    # the lookup above can't see it. Same restore, same reuse:
                    # re-link the copy rather than mint a twin of it.
                    #
                    # Only when this line wasn't showing a set of its own
                    # (`previous is None`). If it was, this blur edits THAT set,
                    # and landing on the copy's values doesn't make it the same
                    # performance: re-linking would fold two sets into one, and
                    # a later clear of the line would delete the survivor. The
                    # lookup above can't take the same gate: its match already
                    # sits on this line, so declining it leaves two identical
                    # rows here, both hidden and both deleted by one clear.
                    if existing is None and previous is None:
                        existing = next(
                            (
                                row
                                for row in log.sets.filter(
                                    source_line__isnull=True,
                                    reclaimed_line=cell,
                                    prescription=line_zero_cell,
                                )
                                if parsing.same_logged_set(
                                    (row.reps, row.load, row.rpe),
                                    (values["reps"], values["load"], values["rpe"]),
                                )
                            ),
                            None,
                        )
                        if existing is not None:
                            # Keeps its pk and set_number; it goes back to being
                            # this line's parsed row, hidden by the line's text.
                            existing.source_line = cell
                            existing.reclaimed_line = None
                            existing.save(
                                update_fields=["source_line", "reclaimed_line"]
                            )
                    if existing is not None:
                        created = existing
                        # It was already logged, so there is nothing to
                        # re-celebrate.
                        previous_values = (
                            existing.reps,
                            existing.load,
                            existing.rpe,
                        )
                    else:
                        taken = set(
                            log.sets.filter(prescription=line_zero_cell).values_list(
                                "set_number", flat=True
                            )
                        )
                        # #570: bounded by the SAME helper `athlete_log_session`
                        # uses. This was the fourth writer of a `set_number` and
                        # the only one still unbounded, so an exercise already
                        # carrying the full legal range could mint a row at 51+ —
                        # a number `_clean_logged_sets` rejects and
                        # `presenters._set_rows` no longer renders, i.e. a set
                        # that counts toward records while being invisible and
                        # unpostable.
                        #
                        # `None` means nothing in the legal range is free. A
                        # line that just replaced a row of its OWN can still
                        # land, on the number that row freed a moment ago —
                        # including one above the ceiling, which the scan
                        # cannot reach but which this very performance already
                        # occupied. Without that fallback, replacing a row left
                        # at 51+ by the old unbounded walk deleted it and
                        # created nothing: a performed set destroyed by an
                        # ordinary edit, on a 200 response. Still
                        # re-checked against `taken` rather than trusted: this
                        # is the one place that decides a number, and a freed
                        # one is only free while nothing else has taken it.
                        #
                        # Still `None` after that means there is genuinely
                        # nowhere to put the row, and none is created. The
                        # cell's text is saved either way, and
                        # `sub_line_warn_reason` reports the line as unlogged —
                        # which is exactly true.
                        number = _first_free_set_number(taken, cell.line)
                        if number is None:
                            number = next(
                                (n for n in freed_numbers if n not in taken), None
                            )
                        if number is not None:
                            created = LoggedSet.objects.create(
                                session_log=log,
                                prescription=line_zero_cell,
                                # #578 C1: written alongside `prescription`,
                                # not instead of it — see
                                # `LoggedSet.exercise_slot`'s model comment.
                                exercise_slot_id=line_zero_cell.exercise_slot_id,
                                source_line=cell,
                                set_number=number,
                                **values,
                            )
                            is_new_set = previous is None
                    # Reps and load ONLY. The record is derived from those two
                    # (`personal_records._performed_sets` never reads RPE), so
                    # including RPE here made a pure RPE correction —
                    # ``120 x 5, RPE 8`` to ``RPE 9`` — look like a new
                    # performance: the row is deleted and recreated with a fresh
                    # pk, so the toast filter matched, and the athlete was
                    # congratulated a second time for the identical e1RM.
                    # Compared as VALUES, not strings: `120` and `120.0` are one
                    # record, so spelling one of them differently re-fired a 🎉
                    # already celebrated.
                    # `created is not None` guards the bounded walk above
                    # declining to mint a row (#570) — with no row there is no
                    # value to compare, and nothing to re-celebrate either.
                    unchanged = (
                        created is not None
                        and previous_values is not None
                        and parsing.same_logged_set(
                            previous_values[:2], (created.reps, created.load)
                        )
                    )

            # This blur left no set on the cell, so the log may now hold
            # nothing. An earlier version scoped this to "there WAS a set before"
            # — too narrow: a first blur whose values overrun the column limits
            # creates the log, then declines to insert, and left an empty one
            # behind. The invariant is simply that an empty log is noise.
            if created is None and _reap_empty_pending_log(log):
                return []

            # Editing a cell on an already-DONE log changes the very sets the
            # persisted AthleteOneRm is derived from, so it has to be recomputed
            # — otherwise blanking a 150 x 5 (or adding a heavier set) leaves a
            # stale estimate driving percent-load suggestions and the coach's
            # designer until some later structured save happens to fix it.
            # Gated on DONE because derivation is DONE-only by design; a PENDING
            # log has nothing to promote yet (5b's settle does that).
            if log.status == SessionLog.Status.DONE:
                # Always include this cell's own lift. `trainable_cells()` is a
                # session-wide list that can omit the very row just edited, and
                # the refresh has to cover the lift whose sets actually changed.
                # (This originally guarded the skipped case, where the delete
                # stripped a set from a non-trainable row; a skipped row is now
                # read-only to this path, but the belt-and-braces include is
                # still correct and costs one list append.)
                cells = list(session.trainable_cells())
                if not any(c.pk == line_zero_cell.pk for c in cells):
                    cells.append(line_zero_cell)
                meso_one_rm.refresh_one_rms(
                    athlete,
                    cells,
                    session.week.mesocycle.plan.unit,
                )

        if is_new_set:
            track(EventName.SET_LOGGED, actor=athlete, subject=log, via="typed")

        # The toast read gets its OWN savepoint, deliberately. Inside the one
        # above, a failure here would roll back the upsert with it — throwing
        # away a perfectly good parsed set because a cosmetic 🎉 lookup broke,
        # and that set would then only ever come back if the athlete happened
        # to edit this same cell again. Separated, the upsert is already
        # committed and a failed read costs nothing but the toast.
        with transaction.atomic():
            # Scoped to THIS blur's set. `new_records_in` reports every lift in
            # the session that beats its prior best, so an unscoped read would
            # re-return a PR won on an earlier line every time the athlete
            # blurred an unrelated note — re-firing the same 🎉 over and over.
            # No set written (blank/skip/swap/note) means nothing to celebrate.
            #
            # `unchanged` suppresses a re-blur that altered nothing. The upsert
            # deletes and recreates, so the row always has a FRESH pk — the
            # `created.pk` filter alone therefore matched every time, and simply
            # focusing and leaving a PR-winning cell re-fired the 🎉 for work
            # already celebrated. A blur is only news if the values moved.
            if created is not None and not unchanged:
                new_records = [
                    r for r in new_records_in(log) if r.logged_set_id == created.pk
                ]
    except Exception:
        logger.exception(
            "parse-at-commit: failed to upsert a LoggedSet for cell %s "
            "(session=%s, athlete=%s); the cell's text was preserved.",
            cell.pk,
            session.pk,
            athlete.pk,
        )
    return new_records


def _first_free_set_number(taken, start):
    """The lowest set number in ``1..MAX_LOGGED_SET_NUMBER`` not in ``taken``, or ``None``.

    #570: bounds ``athlete_log_session``'s collision renumbering. Tries upward
    from ``start`` first, which preserves the old walk's "fall through to the
    next free number" behavior and is the ordinary case (a row nudged aside by
    a slot or two). Only when nothing is free between ``start`` and the cap
    does it scan the whole range from the bottom, so a hole BELOW ``start`` —
    freed by an earlier delete in this same save, say — is still found rather
    than a save being refused that could have succeeded. ``None`` means every
    number in the legal range is genuinely taken.
    """
    # `max(start, 1)` keeps the first scan inside the range this docstring
    # promises. No caller passes less than 1 today, but the helper is the one
    # place that decides what a legal number is, so it says so itself.
    for number in range(max(start, 1), MAX_LOGGED_SET_NUMBER + 1):
        if number not in taken:
            return number
    for number in range(1, MAX_LOGGED_SET_NUMBER + 1):
        if number not in taken:
            return number
    return None


def _names_live_row(cleaned_set, live_rows):
    """Does ``cleaned_set["id"]`` ANCHOR to a row THIS log holds, under the SAME prescription (#567/#568 P1-B, P1-G)?

    ``live_rows`` is the PRE-DELETE snapshot ``athlete_log_session`` takes
    before it deletes or renumbers anything this save — see the comment
    there — mapping each pk to that row's OWN ``prescription_id``. An id in
    that snapshot, under the prescription its own row actually belongs to, is
    ANCHORED: real evidence about a row this very save is looking at (even
    one it is about to replace, since the payload naming it is exactly the
    payload doing the replacing). Anything else is treated exactly the same
    as "no live row at all":

    * an id NOT in the snapshot is STALE — the row it once named is already
      gone, most plausibly because a write-ahead body is replaying after its
      first delivery already committed and moved that row's data under a new
      pk — and a stale id is not weaker evidence, it is NO evidence,
      indistinguishable from an id the client made up;
    * an id that IS in the snapshot, but under a DIFFERENT prescription than
      the one ``cleaned_set`` claims (#567/#568 P1-G), is likewise no
      evidence for THIS claim — a crafted payload can pair a real, live pk
      with the wrong lift, and pk equality alone must not let it borrow that
      row's identity. Before this fold, that case was classified anchored,
      failed a SEPARATE prescription check at each of the three call sites,
      and simply ``continue``d without ever trying the positional fallback —
      degrading to "no match at all" instead of "stale id, try position",
      exactly the failure the stale-id rule exists to avoid. Folding the
      agreement in here means an id under the wrong prescription now falls
      through to the very same positional path a stale id does, with no
      separate check needed at any call site.

    ``_client_held``, the twin absorb in ``athlete_log_session``, and
    ``_consume_carried_link`` all fall back to today's positional match
    whenever this returns ``False``, rather than treating it as "no match",
    because position is the only evidence left behind — exactly what the
    id-less path already runs on.
    """
    row_id = cleaned_set["id"]
    return (
        row_id is not None and live_rows.get(row_id) == cleaned_set["prescription_id"]
    )


def _client_held(row, cleaned_sets, identified, live_rows):
    """Did this save's payload actually come from a page showing ``row``?

    Only asked of a VISIBLE parsed row — one a coach rewrite surfaced after the
    athlete's page had loaded. The replace-delete needs to know whether the
    client was looking at it, and the payload is the only evidence there is.

    IDENTIFIED (#567), ANCHORED id (``_names_live_row``, #568 P1-B, P1-G):
    pure id — ``cs["id"] == row.pk``. The prescription agreement (P2-A) is
    already part of what "anchored" means (folded into ``_names_live_row``
    itself, P1-G) — nothing further to check here. The id is the client's own
    proof it rendered this exact row (``serialize_session_log`` handed it
    out, and a current client only ever posts one it was given), which is
    strictly stronger evidence than the values match below — so there's
    nothing left for a value check to rule out... with one exception (P1-A):
    a WHOLLY BLANK posted set (``reps``, ``load`` and ``rpe`` all ``""``) is
    not evidence the client saw this row's VALUES, because an empty,
    merely-checked grid row and a row a stale page
    never rendered at all post identically. Counting a blank id match as
    "held" let a bare id — adopted from a response the client's own stale page
    never asked for, see ``syncFromLog``'s "no match" comment — delete a row
    that still carried real values and replace it with nothing. So a blank
    posted set counts as held ONLY when the row itself is also blank; the
    moment the row carries any of the three, a blank id match is treated as NO
    match, and the row falls through to being spared, same as any other id
    that names nothing here. This guard applies HERE only — the twin absorb
    and ``_consume_carried_link`` already require exact value equality via
    ``same_logged_set``, so a blank posted set naturally fails to match a
    non-blank row there with no special-casing needed.
    Consequence, worth stating plainly, for the ordinary (non-blank) anchored
    case: an EDIT to a visible parsed row now REPLACES it (same grid row, one
    performance, new values) — where the positional path below spares an
    id-less edit, since its values no longer match, and leaves the collision
    renumbering to push the old row aside into a visible duplicate. Preferring
    one row over two for the id-known case is the more correct behavior; it
    isn't applied to the id-less path because that path can't tell an edit
    from an athlete typing an unrelated set into the same slot (see the
    docstring below).

    STALE id (#568 P1-B; tagged, but ``_names_live_row`` is false) and
    CLIENT_ID (names no server row by construction, #567 B) both fall through
    to the very same positional test the fully id-less path below uses — see
    ``_names_live_row``'s docstring for why a stale id carries no information,
    and #567 B's own fix for why a ``client_id`` set must never be read as
    holding a row it never claimed to be.

    Positional (id-less; a legacy/stale-tab client, or #567's identified mode
    turned off because the WHOLE payload lacks ids — also the fallback for a
    STALE id or a ``client_id`` within an identified payload, above): posting
    the row's slot is not enough on its own. A parsed row is numbered by its
    sub-line (``cell.line``), and the structured grid numbers its own rows
    from 1, so the two share a numbering space: an athlete typing a different
    set into structured row 1 posts ``(prescription, 1)`` and would have
    looked like proof of seeing a parsed row that also happens to be set 1 —
    and the delete then destroyed a performance nobody asked to change.

    So the payload must RE-STATE the row: same slot, same values. An edit to a
    visible parsed row still replaces it (the athlete posts the slot with new
    values only after the old ones were rendered there — see the collision
    renumbering, which moves the row aside instead). Preferring a visible
    duplicate over a silent deletion is the same call the rest of this slice
    makes.
    """
    for cs in cleaned_sets:
        if identified and cs["id"] is not None:
            if _names_live_row(cs, live_rows):
                if cs["id"] != row.pk:
                    continue
                row_blank = row.reps == "" and row.load == "" and row.rpe == ""
                cs_blank = cs["reps"] == "" and cs["load"] == "" and cs["rpe"] == ""
                if cs_blank and not row_blank:
                    continue  # P1-A: a blank id match is no match at all
                return True
            # STALE (P1-B), or ANCHORED to a DIFFERENT row than `row` (an id
            # naming some other live pk under the wrong prescription, P1-G,
            # or under the RIGHT prescription but a DIFFERENT row entirely):
            # no live pk we can trust for THIS row — fall through to position.
        elif identified:
            continue  # client_id: names no server row, so holds nothing
        if (cs["prescription_id"], cs["set_number"]) == (
            row.prescription_id,
            row.set_number,
        ) and parsing.same_logged_set(
            (row.reps, row.load, row.rpe), (cs["reps"], cs["load"], cs["rpe"])
        ):
            return True
    return False


def _consume_carried_link(carried_links, cleaned_set, identified, live_rows):
    """Claim the reclaim link a just-deleted row carried forward (#541), if any.

    ``carried_links`` (built in ``athlete_log_session``, right before the
    delete it survives) holds one entry per replaced row that still points at
    an open reclaim — either the row's own ``source_line`` (a held VISIBLE
    parsed row) or a ``reclaimed_line`` it already carried in from an earlier
    save.

    IDENTIFIED (#567), ANCHORED id (``_names_live_row``, #568 P1-B, P1-G): a
    cleaned set claims a link by the replaced row's own pk
    (``candidate["row_pk"]``) rather than the slot it used to occupy — the
    renumbering elsewhere in this same save can move a hidden row's slot out
    from under a stale repost, but never its pk. The prescription agreement
    (P2-A) is already part of what "anchored" means (folded into
    ``_names_live_row`` itself, P1-G), so there's nothing further to check
    here — a crafted payload naming a real pk under the WRONG prescription
    never reaches this branch at all; it degrades to the positional branch
    below, same as a stale id. The value check stays regardless of mode: same
    as ``_client_held``'s call, an id match with DIFFERENT values is an edit,
    not a restore, and #541's rule ("a later save carries the link only when
    the posted row restates it unchanged, and an edit drops it") means an
    edit must get no link.

    STALE id (#568 P1-B, and P1-G's wrong-prescription case) and Positional
    (id-less, unchanged from #541): a cleaned set only inherits the link when
    it exactly restates the row it replaced: same slot (``prescription``,
    ``set_number``) AND the same values — the same test ``_client_held``
    falls back to for a stale id or an id-less payload. A ``client_id`` set
    (names no server row, #567 B) never reaches this loop at all — see below.

    Mutates ``carried_links``, removing the entry it matches, so the same
    reclaim can never be handed to two different cleaned sets.
    """
    if identified and cleaned_set["id"] is None:
        # client_id: names no server row by construction (#567 B), so there
        # is nothing it could have carried forward. Checked up front so a
        # client_id set can never accidentally claim a link by slot+value
        # alone — the very ambiguity #567 exists to remove.
        return None
    anchored = identified and _names_live_row(cleaned_set, live_rows)
    for index, candidate in enumerate(carried_links):
        if anchored:
            if candidate["row_pk"] != cleaned_set["id"]:
                continue
        else:
            # Positional: id-less, unchanged from #541 — and (P1-B) a STALE
            # id's fallback, since a stale id carries no information and
            # position is the only evidence left (see `_names_live_row`).
            if candidate["prescription_id"] != cleaned_set["prescription_id"]:
                continue
            if candidate["set_number"] != cleaned_set["set_number"]:
                continue
        if not parsing.same_logged_set(
            candidate["values"],
            (cleaned_set["reps"], cleaned_set["load"], cleaned_set["rpe"]),
        ):
            continue
        return carried_links.pop(index)["link_id"]
    return None


def _cell_warn_reason_or_blank(cell, line_zero_cell, *, session, athlete):
    """``sub_line_warn_reason`` for the cell-write response, guarded.

    The tolerance guarantee (plan §11) is that a blur never turns into a
    4xx/5xx over a parse problem — but this read happens while BUILDING the
    response. So a parser or database failure here escaped as a 500 even
    though the athlete's text had already committed: the one outcome the
    guarantee exists to rule out, and the harder one to spot because the write
    itself succeeded.

    A tint we cannot compute is reported as no tint. That is the safe
    direction — the cell reloads with the presenter's own, independently
    derived answer — and it is strictly better than losing the response.

    #568: reads the SAME log the presenter reads (``athlete_session``, via
    the shared ``models.newest_session_logs``) and hands
    ``sub_line_warn_reason`` its ``backing_sets`` scoped to that one log,
    rather than letting it fall back to its own unscoped query — which used
    to match a ``LoggedSet`` for this cell on ANY session log in the
    database: a stray older log for this same (session, athlete), or, after a
    coach moves the exercise to another day (``prescription_move``), the day
    it moved FROM. ``session``/``athlete`` are keyword-only so a future
    caller can't pass them positionally and silently swap them with
    ``line_zero_cell``. An empty tuple when there is no log at all — a cell
    can be tinted before the athlete has ever logged anything today, and
    ``sub_line_warn_reason`` treats "no backing rows" and "no log"
    identically (nothing backs the line either way).

    #567/#568 P1-D — called from the RESPONSE construction, OUTSIDE
    ``athlete_cell_write``'s ``transaction.atomic()`` block, not from inside
    it, and with no ``transaction.atomic()`` of its own. A prior version of
    this fix moved the call inside that atomic block (right after
    ``_upsert_parsed_set``, on the reasoning that it should see the row
    upsert just wrote under the SAME transaction) and wrapped this query in
    its own nested savepoint, by analogy with ``_upsert_parsed_set``'s own
    "load-bearing, not decorative" guard. That analogy does not hold, and the
    result was actively dangerous rather than merely unnecessary: a
    *database* failure during this read (a dropped connection, a pgbouncer
    ``server_lifetime`` cycle, a mid-request DB restart) can make the
    savepoint's OWN rollback fail too, which is precisely when Django marks
    the connection ``needs_rollback`` — the ``except Exception`` below
    swallows the re-raised error and returns ``False`` same as always, so
    nothing here ever raises, but the OUTER atomic then exits with no
    exception and ``needs_rollback`` still set, and rolls back SILENTLY. The
    view would return 200 with the cell's text echoed back while
    ``cell.save()`` and the parsed ``LoggedSet`` are gone underneath it — the
    athlete sees a save that "worked", the client drops its offline-queue
    copy believing it landed, and the write is simply lost. Before #568 this
    same class of failure was harmless, because the read ran after COMMIT:
    running here, AFTER the write's transaction has already closed, is what
    guarantees a swallowed failure in this function can never reach back and
    touch a write that already succeeded — the one thing the tolerance
    guarantee (plan §11) exists to protect.

    #567/#568 P2-A — this read is deliberately POST-COMMIT, best-effort, and
    NOT serialized against a concurrent writer. An earlier version of this
    docstring claimed the opposite — that "there is no concurrent write that
    could land in the gap between the transaction that just committed and
    this read", reasoning that the session lock and ``_upsert_parsed_set``'s
    row lock already serialize every writer. That claim is FALSE: both locks
    release AT commit, not after this function returns, and this read then
    runs in autocommit as several separate, unserialized statements — so a
    second writer that was merely PARKED on the session lock is freed by that
    very commit and can run, and finish, before this read even starts. An
    ordinary trigger, not a contrived one: a sub-line has focus while the
    athlete taps "Log session" — the blur's POST and the save's POST are
    concurrent by construction, and ``athlete_log_session`` takes the exact
    same session lock this function's own caller does. The PLACEMENT is still
    correct, and must not move back inside the transaction (P1-D, above) — but
    the reason is narrower than the old claim: this function's only hard
    requirement is that ITS OWN failure can never touch a write that already
    committed. It is a display hint, free to read a moment that has already
    moved on, exactly like the presenter's own next read — the two are not
    required to agree with a THIRD write racing both of them, only with each
    other once each has settled. An overstated justification here is what
    invites the next person to move this call back inside the transaction, on
    the reasoning that "nothing can race it anyway" — which is precisely the
    bug this placement exists to prevent.

    #567/#568 P2-B, updated by #571 — the argument above (that a *database*
    failure here can never roll back a write that already committed) itself
    depends on this call running OUTSIDE any request-level
    ``transaction.atomic()``, not just outside the one this file opens
    explicitly. A per-database ``ATOMIC_REQUESTS`` setting (read by
    ``BaseHandler.make_view_atomic``, ``django/core/handlers/base.py``, off
    ``connections.settings[alias]["ATOMIC_REQUESTS"]``) would wrap every
    view — this one included — in exactly such a transaction, committed only
    if the view returns without raising. At the time P2-B was written, a
    bare module-scope ``ATOMIC_REQUESTS = True`` sat in
    ``config/settings/base.py``, outside ``DATABASES["default"]`` — not the
    place Django reads it from, so it was inert and changed nothing; #571
    deleted that dead line rather than fix it in place, specifically so
    nobody "corrects" its location later without reading this. If a real
    per-database ``ATOMIC_REQUESTS = True`` is ever added under
    ``DATABASES["default"]``, this call would start running inside a
    request-level atomic block, and the ``except Exception`` below would
    leave the connection's ``needs_rollback`` flag set on a database failure
    — silently rolling back the athlete's already-``cell.save()``d text and
    parsed ``LoggedSet`` while ``athlete_cell_write`` still returns 200, the
    exact failure P1-D exists to rule out. #571's own fix — checking
    ``connection.needs_rollback`` from inside ``athlete_cell_write``'s
    explicit ``transaction.atomic()`` block before trusting the response —
    does NOT cover this danger: THIS read runs after that block has already
    exited, so it would be inside a *different*, outer, request-level atomic
    that ``athlete_cell_write`` never sees and cannot guard. Turning
    ``ATOMIC_REQUESTS`` on for real needs every swallowed-failure site
    re-examined, this one included, not just this file's own explicit block
    — see the settings comment at the deleted line for the general warning,
    and treat this paragraph as the specific instance of it.

    #567/#568 P1-H — ``loggable`` is derived from a FRESH read of the line-0
    row here, not from the caller's ``line_zero_cell`` instance.
    ``athlete_cell_write`` builds that instance once, in ``line_zero``,
    *before* its write transaction even opens; ``_upsert_parsed_set`` then
    re-reads the SAME row under ``select_for_update`` and acts on THAT fresh
    value. A coach's ``prescription_unskip`` landing inside this request's
    window — after the stale snapshot was taken but before the locked reread
    — makes this very request log a REAL set under a since-lifted skip, while
    the caller's stale instance still says skipped. Reporting ``warn`` from
    that stale instance then contradicts the set this same request just
    wrote, and disagrees with the next render (the presenter), which reads
    the row fresh. This function already queries the database once, right
    above, for ``log`` — doing it again here, for the row, and only AFTER the
    write's transaction has committed, is what puts this read at exactly the
    same moment the presenter's own next read happens, which is what makes
    the two surfaces agree. The caller's instance is kept only as a fallback
    for the row having vanished entirely between then and now (a history
    restore hard-deleting a stray cell) — see ``_upsert_parsed_set``'s own
    identical fallback for that case.
    """
    try:
        # The shared newest-log rule — see models.newest_session_logs.
        log = newest_session_logs(session, athlete).first()
        backing_sets = (
            tuple(row for row in log.sets.all() if display_line_id(row) == cell.pk)
            if log is not None
            else ()
        )
        fresh_line_zero = Prescription.objects.filter(pk=line_zero_cell.pk).first()
        skipped = (
            fresh_line_zero.skipped
            if fresh_line_zero is not None
            else line_zero_cell.skipped
        )
        # #572: the athlete's rows for this same cell on any OTHER day. Left
        # UNEVALUATED on purpose — `sub_line_warn_reason` touches it only on
        # the one branch that needs it (the text resolves to a set and nothing
        # on THIS day backs it), so an ordinary blur pays for no extra query.
        # The cell pk doesn't change when a coach drags the exercise across
        # days: `prescription_move` re-points the `ExerciseSlot` and the cell
        # travels with it, so the rows left behind still name this cell
        # through `source_line`/`reclaimed_line` while their `SessionLog`
        # stays on the day they were actually logged.
        #
        # Deliberately NOT pinned to one log the way `backing_sets` above is
        # (`-created_at, -pk`, #568): a row stranded on a split/older log, or
        # on a soft-deleted day, still counts toward the athlete's live 1RM
        # and PRs — neither `one_rm.derive_one_rm_values` nor
        # `personal_records._live_logged_sets` filters by log recency or by
        # `session__deleted_at` — so it is still a genuine double-count risk
        # and a repost of this line would still duplicate it. A reviewer
        # proposed adding `session_log__session__deleted_at__isnull=True`
        # here; that would be WRONG for exactly this reason, so don't.
        elsewhere_sets = LoggedSet.objects.filter(
            Q(source_line=cell) | Q(source_line__isnull=True, reclaimed_line=cell),
            session_log__athlete=athlete,
        ).exclude(session_log__session=session)
        return (
            sub_line_warn_reason(
                cell,
                loggable=not skipped,
                backing_sets=backing_sets,
                elsewhere_sets=elsewhere_sets,
            )
            or ""
        )
    except Exception:
        logger.exception(
            "parse-at-commit: failed to derive the warn flag for cell %s; "
            "reporting no warning (the reload derives its own).",
            cell.pk,
        )
        return ""


def _clean_logged_sets(raw_sets, session):
    """Validate the posted ``sets`` against this session, or return a 400.

    Returns ``(cleaned, None)`` on success or ``(None, HttpResponseBadRequest)``.
    Every set must reference a prescription **in this session** (no foreign rows),
    carry a positive integer ``set_number`` (defaulting to its position), and have
    string reps/load/rpe within the model's ``max_length``.

    Row identity (#567): each set may ALSO carry ``id`` (a positive int — the
    ``LoggedSet.pk`` the client rendered in that grid row) or ``client_id`` (a
    non-empty string, at most 64 chars — a client-minted id for a row that has
    no server row yet), but never both — a client always knows which of the two
    describes a given grid row, so one item claiming both is malformed, not
    ambiguous-but-valid. Neither is RESOLVED here: ``id`` is only ever checked
    against ``log.sets`` inside ``athlete_log_session``'s transaction, because
    the row it names can have been deleted by an earlier step of the very save
    that's validating it (a replace, an absorb) — an id that names no row in
    THIS athlete's log for THIS session is simply "no match", exactly as if the
    row were never mentioned. What's enforced here is shape and payload-wide
    uniqueness only: a duplicate ``id`` or ``client_id`` across two items in one
    request is always wrong regardless of what either later resolves to (the
    same reasoning as the existing duplicate ``(prescription, set_number)``
    check above). Every cleaned item carries both keys — ``None`` when the
    client sent neither — so a caller can test for presence without a
    ``dict.get`` default sprinkled at every read site.

    #567 P2-B: a payload that MIXES tagged and untagged sets — some carrying
    ``id``/``client_id``, others neither — is ALSO malformed, and rejected
    here for the same reason as the both-in-one-item case above: no shipped
    client can produce it (a client on this contract tags every set it knows
    how to, always), so it is not a legitimate "partial upgrade" to degrade
    gracefully, only a bug to surface. Silently demoting a payload like that
    to the whole-request positional fallback — which is what happened before
    this check existed, since ``athlete_log_session`` computes ``identified``
    from exactly these cleaned sets — would hide the bug behind the same
    fallback a genuinely old client uses on purpose. Rejecting it here makes
    ``identified`` a VALIDATED property of any payload that reaches
    ``athlete_log_session``, not merely an inferred one: by the time it's
    computed there, every cleaned set is already guaranteed to agree on
    whether this payload tags rows at all.
    """
    if not isinstance(raw_sets, list):
        return None, HttpResponseBadRequest("sets must be a list.")
    # Only trainable rows are postable — the logger never renders a skipped cell.
    allowed_ids = {p.pk for p in session.trainable_cells()}
    cleaned = []
    seen = set()
    seen_ids = set()
    seen_client_ids = set()
    for position, raw in enumerate(raw_sets, start=1):
        if not isinstance(raw, dict):
            return None, HttpResponseBadRequest("Each set must be an object.")
        presc_id = raw.get("prescription")
        # ``bool`` is an ``int`` subclass — reject it explicitly so ``true`` isn't an id.
        if (
            not isinstance(presc_id, int)
            or isinstance(presc_id, bool)
            or presc_id not in allowed_ids
        ):
            return None, HttpResponseBadRequest(
                "Each set must reference a prescription in this session."
            )
        set_number = raw.get("set_number", position)
        if (
            not isinstance(set_number, int)
            or isinstance(set_number, bool)
            or not 1 <= set_number <= MAX_LOGGED_SET_NUMBER
        ):
            return None, HttpResponseBadRequest(
                f"set_number must be between 1 and {MAX_LOGGED_SET_NUMBER}."
            )
        # Each (prescription, set_number) is logged at most once — duplicates
        # would persist as two rows that the presenter collapses on reload but
        # the agent's grounding still double-counts, breaking idempotency.
        key = (presc_id, set_number)
        if key in seen:
            return None, HttpResponseBadRequest(
                "Duplicate set for the same prescription and set number."
            )
        seen.add(key)

        row_id = raw.get("id")
        client_id = raw.get("client_id")
        if row_id is not None and client_id is not None:
            return None, HttpResponseBadRequest(
                "A set may carry id or client_id, not both."
            )
        if row_id is not None:
            # ``bool`` is an ``int`` subclass, same guard as ``prescription`` above.
            if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
                return None, HttpResponseBadRequest("id must be a positive integer.")
            if row_id in seen_ids:
                return None, HttpResponseBadRequest("Duplicate id in sets.")
            seen_ids.add(row_id)
        if client_id is not None:
            if (
                not isinstance(client_id, str)
                or not client_id.strip()
                or len(client_id) > MAX_CLIENT_ID_LENGTH
            ):
                return None, HttpResponseBadRequest(
                    "client_id must be a non-empty string of at most "
                    f"{MAX_CLIENT_ID_LENGTH} characters."
                )
            if client_id in seen_client_ids:
                return None, HttpResponseBadRequest("Duplicate client_id in sets.")
            seen_client_ids.add(client_id)

        fields = {}
        for field, max_length in LOG_SET_FIELDS.items():
            value = raw.get(field, "")
            if not isinstance(value, str):
                return None, HttpResponseBadRequest(f"{field} must be a string.")
            if len(value) > max_length:
                return None, HttpResponseBadRequest(f"{field} is too long.")
            fields[field] = value
        cleaned.append(
            {
                "prescription_id": presc_id,
                "set_number": set_number,
                "id": row_id,
                "client_id": client_id,
                **fields,
            }
        )
    # #567 P2-B: every set must agree on whether this payload tags rows at
    # all — see the docstring. Checked once, over the whole cleaned list,
    # rather than per-item above: only here do we know every item's verdict.
    if cleaned:
        tagged = [cs["id"] is not None or cs["client_id"] is not None for cs in cleaned]
        if any(tagged) and not all(tagged):
            return None, HttpResponseBadRequest(
                "Every set must carry id or client_id, or none may."
            )
    return cleaned, None


# -- Athlete PWA: manifest, service worker, offline shell (Phase 4b — S7) --
#
# The athlete surface is an installable, offline-tolerant PWA. The manifest and
# service worker are served as *views* (not static files) for two reasons:
#   1. the static pipeline (``CompressedManifestStaticFilesStorage``) hashes
#      filenames, which would give the worker an unstable URL across deploys; and
#   2. a service worker only controls pages at or below its own path, so it must
#      be served from ``/meso/sw.js`` to control ``/meso/me/``.
# The worker is rendered from a template that resolves the *hashed* asset URLs via
# ``{% static %}`` at render time, so its precache list stays valid every deploy.

PWA_THEME_COLOR = "#31759d"  # shared site accent (base.css --accent, steel-blue)
PWA_BACKGROUND_COLOR = "#f4f4f5"  # meso app background (meso.css --bg)


@require_GET
def manifest_webmanifest(request):
    """The web-app manifest — the browser's install descriptor (S7).

    Public (the browser fetches it before any session). Launches into the
    athlete home, scoped to ``/meso/`` so only the athlete surface is the app.
    """
    data = {
        "name": "Meso — Training",
        "short_name": "Meso",
        "description": (
            "Your coach's training plan — log every session, even offline."
        ),
        "start_url": reverse("meso:athlete_home"),
        "scope": "/meso/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": PWA_THEME_COLOR,
        "background_color": PWA_BACKGROUND_COLOR,
        "icons": [
            {
                "src": static("png/meso-icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("png/meso-icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": static("png/meso-icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


# Bumped when the cached shell changes so the worker drops stale caches on
# activate. Keep in sync with the cache name baked into the worker template.
# v2: added meso_onboarding.js to the precached shell (first-time UX Phase 4).
# v3: re-skinned meso.css to the shared steel-blue accent (design-system PR 3).
# v4: meso_athlete.js queues lines typed offline (#527). Cached session pages
#     still point at the old logger, which loses them; activation drops them.
# v6: meso_athlete.js posts row identity with every set (#567). An installed
#     PWA running the cached logger posts no `id`/`client_id`, so the server
#     falls back to matching by `(prescription, set_number, values)` — the very
#     guess that loses and duplicates sets here. Activation drops the stale
#     shell so installed clients pick the new logger up.
# v7: meso_athlete.js reads the cell response's `warn_reason` and stops
#     re-posting a line tinted only because its set is on the day a coach moved
#     the exercise from (#572). A cached logger ignores the new key and keeps
#     re-posting, which is what mints a second LoggedSet for one performance.
PWA_CACHE_VERSION = "meso-pwa-v7"


@require_GET
def service_worker(request):
    """Serve the athlete service worker from ``/meso/sw.js`` (S7).

    Rendered from a template so its precache list can reference the hashed
    static URLs (``{% static %}``). ``Service-Worker-Allowed`` is set explicitly
    even though the served path already scopes it to ``/meso/``.
    """
    body = render_to_string(
        "meso/sw.js",
        {
            "cache_version": PWA_CACHE_VERSION,
            "offline_url": reverse("meso:offline"),
            "home_url": reverse("meso:athlete_home"),
            "static_url": settings.STATIC_URL,
        },
        request=request,
    )
    resp = HttpResponse(body, content_type="text/javascript")
    resp["Service-Worker-Allowed"] = "/meso/"
    # The worker itself must never be served stale, or a new shell can't ship.
    resp["Cache-Control"] = "no-cache"
    return resp


class OfflineView(TemplateView):
    """The offline fallback the worker caches on install (S7).

    Deliberately login-free: the worker pre-caches it on a cold load, so it must
    render for an anonymous fetch rather than redirect to login (a cached login
    redirect would be a useless fallback).
    """

    template_name = "meso/offline.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "training"
        return ctx


# -- Athlete web push: subscribe / unsubscribe (Phase 4b — S3/S7) ----------


def _is_safe_push_endpoint(endpoint):
    """A plausible browser push endpoint: HTTPS to a public host.

    Hardens the SSRF surface — the stored endpoint is later fetched server-side
    by ``pywebpush`` during delivery, so reject the obvious internal targets
    (non-HTTPS, ``localhost``, private/loopback/link-local/reserved IP literals)
    before persisting. A DNS name is accepted (real push services are named
    hosts); name→private-IP rebinding is out of scope for this gate.
    """
    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname
    if host.lower() == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname, not an IP literal — accept
    # One property covers every non-public range (private, loopback, link-local,
    # reserved, CGNAT 100.64/10, documentation, …) — stricter than enumerating.
    return ip.is_global


@login_required
@require_POST
def push_subscribe(request):
    """Store the logged-in athlete's push subscription (upsert by endpoint).

    Body is the browser ``PushSubscription`` JSON (``endpoint`` + ``keys.p256dh``
    / ``keys.auth``). The endpoint is unique: re-subscribing (or a different user
    on the same device) reassigns the row to the current athlete. Validated
    before any write — a malformed body, or an endpoint that isn't HTTPS to a
    public host (SSRF guard), is a 400.
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    endpoint = payload.get("endpoint")
    keys = payload.get("keys")
    if not isinstance(endpoint, str) or not endpoint:
        return HttpResponseBadRequest("endpoint is required.")
    if not isinstance(keys, dict):
        return HttpResponseBadRequest("keys is required.")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not p256dh:
        return HttpResponseBadRequest("keys.p256dh is required.")
    if not isinstance(auth, str) or not auth:
        return HttpResponseBadRequest("keys.auth is required.")
    if len(endpoint) > 512 or len(p256dh) > 255 or len(auth) > 255:
        return HttpResponseBadRequest("Subscription fields are too long.")
    if not _is_safe_push_endpoint(endpoint):
        return HttpResponseBadRequest("endpoint must be an https URL to a public host.")

    # push_subscribed analytics (#509): ``meso_push.js`` re-POSTs the same
    # subscription on every page load while permission stays granted, so only
    # a NEW endpoint, or one changing hands to a different athlete, counts.
    prior_owner = (
        PushSubscription.objects.filter(endpoint=endpoint)
        .values_list("athlete_id", flat=True)
        .first()
    )
    subscription, _created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"athlete": request.user, "p256dh": p256dh, "auth": auth},
    )
    if prior_owner != request.user.pk:
        track(EventName.PUSH_SUBSCRIBED, actor=request.user, subject=subscription)
    return JsonResponse({"ok": True}, status=201)


@login_required
@require_POST
def push_unsubscribe(request):
    """Drop the athlete's own subscription by endpoint (best-effort).

    Scoped to the caller's rows: an athlete can only remove their own
    subscriptions. An unknown endpoint is a quiet success (idempotent).
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return HttpResponseBadRequest("endpoint is required.")
    PushSubscription.objects.filter(athlete=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


# -- invite / relationship actions ----------------------------------------
#
# Tokened POST endpoints. Email delivery of these links is a follow-up; the
# state machine and authorization live here now. Each action is restricted to
# the party entitled to take it (recipient for accept/decline, either party for
# end), independent of who holds the token URL.


@login_required
@require_POST
def invite_accept(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_pending or request.user != link.recipient():
        return HttpResponseForbidden("You cannot respond to this invite.")
    # Seat gate (D4): activating this link consumes one of the coach's seats. A
    # free coach at the cap can't accept an athlete's request (and can't have a
    # coach-invite they sent accepted) until they upgrade. Worded for whichever
    # side is acting — the coach themselves vs. the athlete accepting the coach.
    if not billing_access.can_add_athlete(link.coach):
        if request.user == link.coach:
            messages.error(request, SEAT_LIMIT_MESSAGE)
        else:
            messages.error(
                request,
                f"{link.coach.display_name()} can't take on new athletes right now.",
            )
        return redirect("meso:roster")
    link.accept()
    messages.success(request, "Relationship accepted.")
    return redirect("meso:roster")


@login_required
@require_POST
def invite_decline(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_pending or request.user != link.recipient():
        return HttpResponseForbidden("You cannot respond to this invite.")
    link.decline()
    messages.success(request, "Invite declined.")
    return redirect("meso:roster")


@login_required
@require_POST
def relationship_end(request, token):
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_active or request.user not in (link.coach, link.athlete):
        return HttpResponseForbidden("You cannot end this relationship.")
    link.end()
    messages.success(request, "Relationship ended.")
    return redirect("meso:roster")


@login_required
@require_POST
def relationship_reinvite(request, token):
    """Coach re-invites a former athlete from the relationship-history surface.

    Reopens the existing closed (ended/declined) ``CoachAthlete`` link to a fresh
    ``pending_coach_invite`` (``CoachAthlete.invite`` rotates the token + clears
    the close timestamps), which the athlete — already a registered user — sees
    on their training home and accepts/declines. Coach-scoped (a foreign token is
    a 404). The seat gate (D4) applies: a free coach at the cap can't re-activate
    a seat they aren't paying for until they upgrade — accepting would create a
    billable seat. A non-closed link (already active/pending) is a friendly no-op.
    Locks the row so a re-invite can't race a concurrent claim. The pending peer
    link is then visible on this page's "Reconnecting" list (surfaced nowhere
    else), so the coach can see where the re-invited athlete went.

    Defense-in-depth: an ended self-link is excluded from this page (its reopen
    path is the roster's "Add yourself as an athlete" affordance), but a
    hand-crafted POST could still hit its token. ``CoachAthlete.invite`` would
    raise ``InvalidTransition`` for a coach == athlete pair, so reopen it the
    same way the roster does instead of 500ing.
    """
    with transaction.atomic():
        # LOCK ORDER (#596) — User precedes CoachAthlete. This must live here,
        # not inside add_self, because the ordinary re-invite branch and its
        # link lock need the same parent-first order too.
        locked_coach = (
            User.objects.select_for_update(no_key=True)
            .filter(pk=request.user.pk)
            .first()
        )
        if locked_coach is None:
            raise Http404("Unknown coach")
        link = get_object_or_404(
            CoachAthlete.objects.select_for_update(),
            token=token,
            coach=request.user,
        )
        if not link.is_closed:
            messages.info(request, "That relationship isn't closed.")
            return redirect("meso:relationship_history")
        if link.is_self:
            CoachAthlete.add_self(request.user)
            messages.success(request, "You're back on your roster.")
            return redirect("meso:relationship_history")
        # Seat gate (D4): accepting the re-invite would consume a billable seat.
        if not billing_access.can_add_athlete(request.user):
            messages.error(request, SEAT_LIMIT_MESSAGE)
            return redirect("meso:relationship_history")
        athlete = link.athlete
        CoachAthlete.invite(coach=request.user, athlete=athlete)
    messages.success(
        request,
        f"Re-invited {athlete.display_name()} — they'll see it on their training home.",
    )
    return redirect("meso:relationship_history")


# -- athlete → coach requests (N4 Phase 2) ---------------------------------
#
# The reverse of the coach email invite: an athlete who already has an account
# asks to train under a coach (CoachAthlete.request → pending_athlete_request).
# The coach accepts/declines it via the recipient views above (invite_accept /
# invite_decline); the athlete may withdraw their own pending request.


@login_required
@require_POST
def athlete_request_coach(request):
    """An athlete asks to train under a coach, found by the coach's email.

    A plain form POST from the athlete's training home. The email is validated
    and resolved to a *coach* (a User with a ``CoachProfile`` — a non-coach or
    unknown address is rejected, as is the requester's own). An already-active
    link is left untouched; an already-pending request is a no-op; otherwise a
    pending request is opened (reopening a previously closed link). The coach is
    notified by email on ``transaction.on_commit``, best-effort — a mail failure
    is logged, never a 500 or a lost request. Always lands back on the home.

    Sandbox gate (S4), both sides of the link: a sandbox *requester* is bounced
    to the roster, and a resolved *target* who is a sandbox coach is treated
    exactly like an unknown email (same flash — a throwaway ``@sandbox.invalid``
    account is not a coach anyone can train under, and the response must not
    leak that the address exists).
    """
    if meso_sandbox.is_sandbox(request.user):
        messages.info(
            request,
            "Invites are disabled in the demo — create a free account to work "
            "with real athletes.",
        )
        return redirect("meso:roster")
    email = CoachInvite.normalize_email(request.POST.get("email"))
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email address.")
        return redirect("meso:athlete_home")
    coach = (
        User.objects.filter(email__iexact=email, coach_profile__isnull=False)
        .exclude(pk=request.user.pk)
        .first()
    )
    if coach is None or meso_sandbox.is_sandbox(coach):
        messages.error(request, "We couldn't find a coach with that email.")
        return redirect("meso:athlete_home")

    with transaction.atomic():
        # ``unique_coach_athlete`` stops a second row, but not a double submit's
        # second ``coach_request_sent`` event and email, or a reopened link's
        # token rotating twice (#540). Lock in ``billing.webhooks._lock_mirror``'s
        # order: the link if it exists, else the athlete's user row, then re-read.
        # ``no_key``: a plain FOR UPDATE would block the commit-time FK KEY SHARE
        # lock of a concurrent insert that references this user — e.g. an invite
        # claim — and deadlock.
        existing = (
            CoachAthlete.objects.select_for_update()
            .filter(coach=coach, athlete=request.user)
            .first()
        )
        if existing is None:
            User.objects.select_for_update(no_key=True).filter(
                pk=request.user.pk
            ).first()
            existing = (
                CoachAthlete.objects.select_for_update()
                .filter(coach=coach, athlete=request.user)
                .first()
            )
        if existing and existing.is_active:
            messages.info(
                request, f"You're already training with {coach.display_name()}."
            )
            return redirect("meso:athlete_home")
        if existing and existing.status == CoachAthlete.Status.PENDING_ATHLETE_REQUEST:
            messages.info(
                request, f"You've already asked to train with {coach.display_name()}."
            )
            return redirect("meso:athlete_home")
        if existing and existing.status == CoachAthlete.Status.PENDING_COACH_INVITE:
            messages.info(
                request,
                f"{coach.display_name()} already invited you — accept it below.",
            )
            return redirect("meso:athlete_home")

        link = CoachAthlete.request(athlete=request.user, coach=coach)
    track(EventName.COACH_REQUEST_SENT, actor=request.user, subject=link)
    athlete = request.user
    roster_url = request.build_absolute_uri(reverse("meso:roster"))

    def _send():
        try:
            sent = send_coach_request_email(
                athlete=athlete, coach=coach, roster_url=roster_url
            )
        except Exception:  # mail is best-effort; never fail the request on it
            logger.exception("Failed to send coach request email to %s", coach.email)
            sent = None
        _flash_send_result(
            request,
            sent=sent,
            success_message=f"Request sent to {coach.display_name()}.",
            target=coach.display_name(),
            bounce_note="Consider telling them about your request directly.",
            noun="Request",
            saved_note="saved",
        )

    transaction.on_commit(_send)
    return redirect("meso:athlete_home")


@login_required
@require_POST
def request_withdraw(request, token):
    """The initiator of a pending link withdraws it (an athlete cancels a request).

    The mirror of ``invite_decline`` (the *recipient* declines): only the party
    who opened the pending link may withdraw it, which marks it declined. Lands
    the athlete back on their home, a coach on the roster.
    """
    link = get_object_or_404(CoachAthlete, token=token)
    if not link.is_pending or request.user != link.initiator():
        return HttpResponseForbidden("You cannot withdraw this request.")
    link.decline()
    messages.success(request, "Request withdrawn.")
    target = "meso:athlete_home" if request.user == link.athlete else "meso:roster"
    return redirect(target)


# -- email invites / onboarding (N4) ---------------------------------------
#
# The coach-initiated, email-addressed onboarding flow: a coach invites a person
# by email (who may not have an account yet), we send a tokened claim link, and
# whoever follows it while authenticated materializes — and immediately activates
# — a CoachAthlete link. Distinct from the peer-invite token views above, which
# act on an existing CoachAthlete between two Users. See docs/archive/meso/invites-plan.md.


def _flash_send_result(
    request, *, sent, success_message, target, bounce_note, noun, saved_note
):
    """Flash the outcome of a best-effort transactional-email send.

    Shared by every ``transaction.on_commit`` callback that sends a
    mail-server email and must not report success while blind to whether the
    message actually went out. ``sent`` is the mail helper's return value
    (``True``/``False``), or ``None`` when the caller caught an exception
    from it. ``AWS_SES_USE_BLACKLIST`` makes a helper return ``False`` for a
    hard-bounced/complained address — without branching on it, a caller was
    told the message went out even when nothing did.

    ``success_message`` is the full flash text for the ``True`` case.
    ``target`` names who we tried (and failed) to email, for the warning
    copy; ``bounce_note`` is the direction-specific suggestion appended to
    the ``False`` warning (e.g. what the sender can do about a paused
    address). ``noun`` ("Invite"/"Request") and ``saved_note`` describe what
    already happened to the underlying row, so the exception copy never
    claims the email went out when it didn't.
    """
    if sent is True:
        messages.success(request, success_message)
    elif sent is False:
        messages.warning(
            request,
            f"Couldn't email {target}: their address previously bounced or "
            f"reported our mail as spam, so sending to it is paused. {bounce_note}",
        )
    else:
        messages.warning(
            request,
            f"{noun} {saved_note}, but the email to {target} could not be "
            "sent right now.",
        )


@login_required
@require_POST
def coach_invite(request):
    """Coach invites an athlete by email → a pending ``CoachInvite`` + claim email.

    A plain form POST from the roster's "Invite an athlete" disclosure. The email
    is validated and normalized; a coach cannot invite their own address; a
    re-invite reuses the open pending row (``open_for``). The claim email is sent
    on ``transaction.on_commit`` and is best-effort — a mail backend failure is
    logged, never a 500 or a lost invite. Always lands back on the roster.

    Sandbox gate (S4): a sandbox coach can't invite a real email address.
    """
    if meso_sandbox.is_sandbox(request.user):
        messages.info(
            request,
            "Invites are disabled in the demo — create a free account to work "
            "with real athletes.",
        )
        return redirect("meso:roster")
    email = CoachInvite.normalize_email(request.POST.get("email"))
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, "Enter a valid email address.")
        return redirect("meso:roster")
    if email == CoachInvite.normalize_email(request.user.email):
        messages.error(request, "You can't invite yourself.")
        return redirect("meso:roster")
    # Seat gate (D4): a free coach at the cap can't open a new invite — accepting
    # it would create a billable seat they aren't paying for.
    if not billing_access.can_add_athlete(request.user):
        messages.error(request, SEAT_LIMIT_MESSAGE)
        return redirect("meso:roster")
    invite, created = CoachInvite.open_for(coach=request.user, email=email)
    track(EventName.INVITE_SENT, actor=request.user, subject=invite, new=created)
    accept_url = request.build_absolute_uri(
        reverse("meso:invite_claim", kwargs={"token": invite.token})
    )
    coach = request.user

    def _send():
        try:
            sent = send_coach_invite_email(
                coach=coach, email=email, accept_url=accept_url
            )
        except Exception:  # mail is best-effort; never fail the invite on it
            logger.exception("Failed to send coach invite email to %s", email)
            sent = None
        _flash_send_result(
            request,
            sent=sent,
            success_message=f"Invite sent to {email}.",
            target=email,
            bounce_note=(
                "Ask them for a different address, or clear it from the "
                "email dashboard if it was a mistake."
            ),
            noun="Invite",
            saved_note="saved",
        )

    transaction.on_commit(_send)
    return redirect("meso:roster")


@login_required
@require_POST
def coach_invite_revoke(request, token):
    """Coach cancels a pending invite they sent. Coach-scoped (foreign → 404).

    Locks the invite row so a revoke and a concurrent claim can't both win — the
    first to acquire the row decides the transition; the loser sees a non-pending
    invite and no-ops.
    """
    with transaction.atomic():
        invite = get_object_or_404(
            CoachInvite.objects.select_for_update(),
            token=token,
            coach=request.user,
        )
        if invite.status in (CoachInvite.Status.PENDING, CoachInvite.Status.EXPIRED):
            invite.revoke()
            messages.success(request, "Invite revoked.")
    return redirect("meso:roster")


@login_required
@require_POST
def coach_invite_resend(request, token):
    """Coach re-arms an outstanding invite they sent (N4 Phase 3).

    Resends a pending **or expired** invite: ``resend`` rotates the token (the
    old emailed link dies), resets the TTL, and brings an expired invite back to
    pending; the fresh claim email goes out best-effort on
    ``transaction.on_commit``. Coach-scoped (a foreign invite is a 404). An
    already-answered invite (accepted/declined/revoked) is a friendly no-op, not
    a 500. Locks the row so a resend can't race a concurrent claim/revoke.

    Sandbox gate (S4): a sandbox coach can't re-arm a real invite.
    """
    if meso_sandbox.is_sandbox(request.user):
        messages.info(
            request,
            "Invites are disabled in the demo — create a free account to work "
            "with real athletes.",
        )
        return redirect("meso:roster")
    with transaction.atomic():
        invite = get_object_or_404(
            CoachInvite.objects.select_for_update(),
            token=token,
            coach=request.user,
        )
        try:
            invite.resend()
        except InvalidTransition:
            messages.info(request, "That invite has already been answered.")
            return redirect("meso:roster")

    email = invite.email
    accept_url = request.build_absolute_uri(
        reverse("meso:invite_claim", kwargs={"token": invite.token})
    )
    coach = request.user

    def _send():
        try:
            sent = send_coach_invite_email(
                coach=coach, email=email, accept_url=accept_url
            )
        except Exception:  # mail is best-effort; never fail the resend on it
            logger.exception("Failed to resend coach invite email to %s", email)
            sent = None
        _flash_send_result(
            request,
            sent=sent,
            success_message=f"Invite resent to {email}.",
            target=email,
            bounce_note=(
                "Ask them for a different address, or clear it from the "
                "email dashboard if it was a mistake."
            ),
            noun="Invite",
            saved_note="refreshed",
        )

    transaction.on_commit(_send)
    return redirect("meso:roster")


@login_required
def invite_claim(request, token):
    """An invited athlete follows the emailed claim link.

    ``@login_required`` bounces an anonymous visitor to ``/accounts/login/`` with
    ``?next=`` back here; allauth carries ``next`` through both login and signup,
    so a brand-new athlete returns authenticated. GET renders a confirm page; POST
    ``action=accept`` materializes an active ``CoachAthlete`` link and lands on the
    athlete's training home, ``action=decline`` marks the invite declined.
    Bearer-token authorized — any authenticated user holding the token may claim
    (no email match; see ``CoachInvite``). An already-answered invite is a friendly
    no-op, never a crash.

    The POST transition runs under a row lock on the invite so two concurrent
    claims (or a claim racing a revoke) can't both pass the pending check and each
    materialize a link — the first to acquire the row wins; the loser sees a
    non-pending invite and no-ops.

    Sandbox gate (S4): the claim is bearer-token authorized, so a visitor still
    logged in as a throwaway sandbox account would bind a real coach to a
    disposable athlete the expiry sweep later deletes. End the sandbox session
    and retry the same URL anonymously — ``login_required`` then routes them
    through login/signup with ``?next=`` back here, exactly like any logged-out
    invitee. (No flash: session storage doesn't survive the logout.)
    """
    if meso_sandbox.is_sandbox(request.user):
        logout(request)
        return redirect(request.get_full_path())
    invite = get_object_or_404(CoachInvite, token=token)
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in ("accept", "decline"):
            return HttpResponseBadRequest("action must be 'accept' or 'decline'.")
        invite_coach_id = invite.coach_id
        with transaction.atomic():
            locked_user_ids = None
            if action == "accept":
                # LOCK ORDER (#596) — CoachAthlete has two User parents, so
                # reserve both in ascending pk before the invite row. A User
                # cascade takes its parent first and only then deletes the
                # CoachInvite; matching it here removes the reverse edge.
                expected_user_ids = {invite_coach_id, request.user.pk}
                locked_user_ids = set(
                    User.objects.select_for_update(no_key=True)
                    .filter(pk__in=expected_user_ids)
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
            # Lock by the *submitted token*, not the pk: a resend that rotated the
            # token out from under this in-flight claim must invalidate the old
            # link (Phase-3 "resend kills the previous token"), so a superseded
            # token finds no row → 404 rather than accepting on stale authority.
            invite = get_object_or_404(
                CoachInvite.objects.select_for_update(), token=token
            )
            if action == "accept" and (
                invite.coach_id != invite_coach_id
                or locked_user_ids != expected_user_ids
            ):
                raise Http404("Invite participants changed")
            if not invite.is_pending:
                messages.info(request, "This invite has already been answered.")
                return redirect("meso:athlete_home")
            if invite.is_expired:
                invite.expire()
                messages.info(
                    request,
                    "This invite has expired. Ask your coach to resend it.",
                )
                return redirect("meso:athlete_home")
            if action == "accept":
                # Seat gate (D4): claiming materializes an active link — a billable
                # seat for the coach. A coach who has since hit their cap can't take
                # on the athlete until they upgrade; the athlete sees why.
                if not billing_access.can_add_athlete(invite.coach):
                    messages.error(
                        request,
                        f"{invite.coach.display_name()} has reached their athlete "
                        "limit and can't add you right now.",
                    )
                    return redirect("meso:athlete_home")
                try:
                    invite.accept(request.user)
                except InvalidTransition as exc:
                    messages.error(request, str(exc))
                    return redirect("meso:roster")
                track(EventName.INVITE_ACCEPTED, actor=request.user, subject=invite)
                messages.success(
                    request,
                    f"You're now training with {invite.coach.display_name()}.",
                )
                return redirect("meso:athlete_home")
            invite.decline()
        messages.success(request, "Invite declined.")
        return redirect("meso:athlete_home")
    # Lazily age out an overdue link on view so the confirm page shows the
    # "expired" state (and the status sticks) rather than offering a dead Accept.
    # The cheap pre-check avoids locking on every GET; the real transition runs
    # under a row lock + re-check (like the POST path), reloading by the
    # *submitted token* so a concurrent resend — which rotates the token and
    # resets the clock — invalidates this stale link (→ 404) instead of letting
    # us render the claim form with, and leak, the freshly rotated token.
    if invite.is_pending and invite.is_expired:
        with transaction.atomic():
            invite = get_object_or_404(
                CoachInvite.objects.select_for_update(), token=token
            )
            if invite.is_pending and invite.is_expired:
                invite.expire()
    return render(
        request,
        "meso/invite_claim.html",
        {
            "invite": invite,
            "coach_name": invite.coach.display_name(),
            "is_self": request.user == invite.coach,
        },
    )


# -- designer autosave API (Phase 3) --------------------------------------
#
# Plain JSON endpoints (no DRF) the designer grid POSTs edits to. Every call is
# scoped to a plan the requester coaches over an *active* relationship — an
# existing-but-unowned plan is a 403, never a silent no-op (N2 / the plan's
# "non-owner POST → 403"). Children (prescription, session) must belong to that
# plan, or it's a 404.

# The one freeform cell field the grid edits (text-first, Phase 2a), with a
# sanity cap — the model column is unbounded ``TextField``, but a cell is a
# spreadsheet cell, not a document. ``name``/``exercise`` are NOT here (P0
# fixed-lineup cutover): a row's name is its ``ExerciseSlot``'s (block-wide
# identity), handled by ``prescription_patch``'s own ``name`` branch.
PATCHABLE_FIELDS = {
    "text": 2000,
}

# Per-EXERCISE row columns (Phase 2a, D2) writable via ``exercise_slot_patch``,
# mapped to a length cap (``note`` is a TextField; same spreadsheet-cell cap
# rationale as ``text`` above).
SLOT_PATCHABLE_FIELDS = {
    "tempo": 64,
    "rest": 64,
    "note": 2000,
}

# Sub-line stacks are short (an RPE row, a cue or two, logged deviations) —
# cap the line index so a buggy client can't fabricate a huge stack.
MAX_CELL_LINE = 20


def _coach_plan_or_forbidden(request, plan_id):
    """The plan the requester coaches, or an ``HttpResponseForbidden``.

    404 when the plan does not exist; 403 when it exists but the requester may
    not edit it — its coach over an active relationship
    (``Plan.is_editable_by``).
    """
    plan = get_object_or_404(Plan, pk=plan_id)
    if not plan.is_editable_by(request.user):
        return None, HttpResponseForbidden("You do not own this plan.")
    return plan, None


def _editable_plan_or_response(request, plan_id):
    """The plan the requester may *edit*, or an error response (S6 Phase 3, D6).

    Ownership first (``_coach_plan_or_forbidden`` → 404/403), then the billing
    gate: a coach over their seat limit after a downgrade gets a 402 instead of
    mutating — they keep read access but can't change or deliver a program until
    back within the cap or re-subscribed. The freeze is **per athlete** (S6 Phase
    5, ``can_edit_plan``): only the soft-suspended links — the active ones beyond
    the oldest ``FREE_SEAT_LIMIT`` — are frozen; the kept athletes stay editable.
    """
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return None, forbidden
    if not billing_access.can_edit_plan(plan):
        return None, _over_limit_json()
    return plan, None


def _body_week_id(request):
    """A designer write's optional ``week_id``, parsed from the JSON request body.

    The real callers post ``application/json`` (``apiPost`` and the deliver
    ``fetch`` always set it, even for an empty ``body: null``); a bodyless / form /
    multipart post carries no ``week_id`` → fall back to the live week. A declared
    JSON body, though, is validated strictly: returns ``(None, HttpResponseBadRequest)``
    when it's malformed (bad JSON, not an object, or a non-integer ``week_id``) so a
    truncated / tampered request that meant to pin a week fails loudly rather than
    silently acting on the live week (which, for deliver, would email/push the wrong
    week). On success returns ``(week_id, None)`` — ``week_id`` is None when absent.
    ``week_id`` arrives from JSON, not an ``<int:...>`` URL segment, so the int
    coercion also guards the pk query against a 500.
    """
    if request.content_type != "application/json" or not request.body:
        return None, None
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, HttpResponseBadRequest("Expected a JSON object.")
    if not isinstance(payload, dict):
        return None, HttpResponseBadRequest("Expected a JSON object.")
    week_id = payload.get("week_id")
    if week_id is None:
        return None, None
    # A real client sends a JSON integer; accept only that. ``int()`` would
    # silently coerce ``1.9``→1 or ``True``→1 onto a valid pk — the exact
    # wrong-week action this strict path exists to reject. ``bool`` is an ``int``
    # subclass, so exclude it explicitly.
    if not isinstance(week_id, int) or isinstance(week_id, bool):
        return None, HttpResponseBadRequest("week_id must be an integer.")
    return week_id, None


def _touch_plan(plan):
    """Bump the plan's ``modified`` so it reads as the coach's working plan.

    The autosave/deliver endpoints write *child* rows (prescriptions, weeks),
    which would otherwise leave ``Plan.modified`` stale — and ``_coach_working_plan``
    orders the bare designer/deliver redirect target by it. ``modified`` is
    ``auto_now``, so saving the field stamps it now.
    """
    plan.save(update_fields=["modified"])


def _cell_or_404(plan, pk):
    """A live ``Prescription`` cell of ``plan`` by pk, or ``Http404`` (P0).

    The fixed-lineup analogue of the old flat per-week prescription lookup: a
    cell is live iff its ``ExerciseSlot``, that slot's ``SessionSlot``, and
    its own ``Week`` are all live, and the slot's mesocycle belongs to ``plan``.
    """
    return get_object_or_404(
        Prescription,
        pk=pk,
        exercise_slot__session_slot__mesocycle__plan=plan,
        exercise_slot__deleted_at__isnull=True,
        exercise_slot__session_slot__deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )


def _session_for_cell(cell):
    """The live (week × day) ``Session`` a cell belongs to (P0).

    A cell has no ``.session`` of its own anymore — its day is the live
    ``Session`` joining its own ``.week`` to its ``ExerciseSlot``'s
    ``SessionSlot``.
    """
    return Session.objects.get(
        week=cell.week, session_slot=cell.exercise_slot.session_slot
    )


@login_required
@require_POST
def prescription_patch(request, plan_id, pk):
    """Patch one prescription cell (or a small batch of cells)."""
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    updates = {}
    for field, max_length in PATCHABLE_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            return HttpResponseBadRequest(f"{field} must be a string.")
        if len(value) > max_length:
            return HttpResponseBadRequest(f"{field} is too long.")
        updates[field] = value

    # ``name`` is identity — the block-shared ``ExerciseSlot``'s (P0 fixed
    # lineup; the one-week swap fields are gone, Phase 2a — a substitution is
    # sub-line text now). The React client echoes the name on every blur, so
    # treat it as an edit only when it actually differs.
    name_edit = None
    if "name" in payload:
        value = payload["name"]
        if not isinstance(value, str):
            return HttpResponseBadRequest("name must be a string.")
        if len(value) > 255:
            return HttpResponseBadRequest("name is too long.")
        if value != cell.name:
            name_edit = value

    if updates or name_edit is not None:
        with transaction.atomic():
            record_plan_action(plan, f"Edited {cell.name or 'exercise'}")
            if updates:
                for field, value in updates.items():
                    setattr(cell, field, value)
                cell.save(update_fields=list(updates))
            if name_edit is not None:
                cell.exercise_slot.name = name_edit
                cell.exercise_slot.save(update_fields=["name"])
            _touch_plan(plan)
    # Row-level reply + refreshed history: this endpoint records an undo action
    # but doesn't re-serialize the plan, so without `history` the client's undo
    # affordance would stay stale until the next full envelope.
    return JsonResponse(
        {
            "ok": True,
            "prescription": serialize_prescription(cell),
            "history": serialize_plan_history(plan),
        }
    )


@login_required
@require_POST
def prescription_delete(request, plan_id, pk):
    """Soft-delete one exercise row — block-wide (P0 fixed-lineup cutover).

    A row's identity is now the ``ExerciseSlot`` shared across every week, so
    removing it removes the row from the **whole block**, not just the viewed
    week (the old per-week semantics). The cell (and its slot/week) must be
    live, or this 404s — including a double-delete of the same row. Response
    is pinned to the cell's own week, not necessarily the plan's current one,
    so the client reopens onto the grid it was editing.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    week = cell.week
    with transaction.atomic():
        record_plan_action(plan, f"Deleted {cell.name or 'exercise'}")
        cell.exercise_slot.soft_delete()
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _new_block_wide_row(session, *, week_id_only=None):
    """Create one ``ExerciseSlot`` + a starter cell for every live week (P0/P2).

    Shared by ``session_add_exercise``'s two paths (unscoped "add exercise" and
    the P2 ``week_id``-scoped "add this week"): both create the same block-wide
    row — one ``ExerciseSlot`` on the day's ``SessionSlot`` plus one starter
    ``Prescription`` cell per live week of the mesocycle — so the loop lives
    here once. ``week_id_only`` is the P2 exception: when given, every created
    cell is ``skipped=True`` except that week's (the new row trains only that
    one week); when ``None`` (the plain unscoped add), every cell trains.
    Returns ``(exercise_slot, cells_by_week_id)``.
    """
    slot = session.session_slot
    next_order = (
        slot.exercise_slots.filter(deleted_at__isnull=True).aggregate(m=Max("order"))[
            "m"
        ]
        or 0
    ) + 1
    exercise_slot = ExerciseSlot.objects.create(
        session_slot=slot, name="New exercise", order=next_order
    )
    cells_by_week = {}
    for week in Week.objects.filter(
        mesocycle=session.week.mesocycle, deleted_at__isnull=True
    ):
        skipped = week_id_only is not None and week.pk != week_id_only
        # A new row's cells start BLANK (Phase 2a): spreadsheet parity — the
        # coach types whatever notation they use, no seeded numbers.
        cells_by_week[week.pk] = Prescription.objects.create(
            exercise_slot=exercise_slot,
            week=week,
            skipped=skipped,
        )
    return exercise_slot, cells_by_week


@login_required
@require_POST
def session_add_exercise(request, plan_id, pk):
    """Append a blank exercise row to a session — block-wide (P0 fixed-lineup cutover).

    Adding a row is now block-wide: creates one ``ExerciseSlot`` on the day's
    ``SessionSlot`` (shared block identity) plus a starter ``Prescription``
    cell on it for EVERY live week of the mesocycle — the new row appears as
    a blank cell across the whole block, not just the viewed week. The reply
    serializes the new slot's cell for the viewed session's own week.

    An optional JSON ``week_id`` (P2 "add this week", issue #440) scopes the
    new row to train only that one week: every created cell is
    ``skipped=True`` except ``week_id``'s. ``week_id`` must resolve to a live
    ``Week`` of THIS session's own mesocycle, or it's a 400 (a nonexistent or
    foreign week is a bad reference, not a 404 — mirrors ``prescription_move``'s
    ``session_id`` convention) — never silently falling back to the unscoped,
    train-everywhere behavior. Omitting ``week_id`` entirely keeps the
    unscoped behavior byte-identical to before P2.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    session = get_object_or_404(
        Session,
        pk=pk,
        week__mesocycle__plan=plan,
        deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    target_week = None
    if week_id is not None:
        target_week = Week.objects.filter(
            pk=week_id, mesocycle=session.week.mesocycle, deleted_at__isnull=True
        ).first()
        if target_week is None:
            return JsonResponse(
                {"ok": False, "error": "week_id must be a live week of this block."},
                status=400,
            )
    with transaction.atomic():
        label = (
            f"Added exercise (Week {target_week.index} only)"
            if target_week is not None
            else "Added exercise"
        )
        record_plan_action(plan, label)
        exercise_slot, cells_by_week = _new_block_wide_row(
            session, week_id_only=target_week.pk if target_week is not None else None
        )
        target_id = target_week.pk if target_week is not None else session.week_id
        cell = cells_by_week.get(target_id)
        _touch_plan(plan)
    # Row-level reply + refreshed history (see prescription_patch).
    return JsonResponse(
        {
            "ok": True,
            "prescription": serialize_prescription(cell),
            "history": serialize_plan_history(plan),
        },
        status=201,
    )


@login_required
@require_POST
def session_add(request, plan_id):
    """Append a blank training day (with a starter row) to the plan — block-wide.

    "Add a day" is now block-wide (P0 fixed-lineup cutover): it creates one
    ``SessionSlot`` (the day's shared identity) plus a ``Session`` instance and
    a starter ``ExerciseSlot``+cell for EVERY live week of the mesocycle — the
    new day appears in every week's grid, not just the one being viewed. An
    optional ``week_id`` in the body pins which week's ``Session`` is returned
    — the multi-week switcher can open a week other than the live one, and the
    reply must reflect where the coach is looking (else a reload shows it on
    the wrong week). It defaults to ``current_week`` for the first-time-UX
    caller that predates the switcher. The week is scoped to the plan (a
    foreign week is a 404). Scoped + edit-gated like the other designer writes
    via ``_editable_plan_or_response`` (403 foreign, 402 over-limit). Returns
    the viewed week's new day in the grid's day shape so the client can append
    it without a reload.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    # An empty body (the pre-switcher callers post none) means "no week_id" —
    # fall back to the live week; a present-but-malformed body is a 400.
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    if week_id is not None:
        week = get_object_or_404(
            Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
        )
    else:
        week = current_week(plan)
    if week is None:
        return HttpResponseBadRequest("This plan has no week to add a day to.")
    meso = week.mesocycle
    # Allocate the next day_number/order under a row lock on the mesocycle (the
    # SessionSlot is block-wide, not per-week) so a double-click or two
    # concurrent submits can't read the same max and create duplicate "Day N"
    # slots. The explicit transaction is required: prod views run in
    # autocommit (ATOMIC_REQUESTS is inert here), so the lock must own its own
    # transaction to be held.
    with transaction.atomic():
        # Lock ordering: plan BEFORE any child row (undo/redo and the deletes,
        # e.g. week_delete, all lock the plan first, then touch weeks) — taking
        # the mesocycle lock first here could deadlock against them.
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        Mesocycle.objects.select_for_update().filter(pk=meso.pk).first()
        # Mirrors ``week_add``'s own indexing (over ALL slots, deleted
        # included) so a soft-deleted day's number/order is never reused.
        agg = meso.session_slots.aggregate(
            max_order=Max("order"), max_day=Max("day_number")
        )
        next_order = (agg["max_order"] or 0) + 1
        next_day = (agg["max_day"] or 0) + 1
        record_plan_action(plan, f"Added Day {next_day}")
        slot = SessionSlot.objects.create(
            mesocycle=meso,
            day_number=next_day,
            name=f"Day {next_day}",
            order=next_order,
        )
        live_weeks = list(Week.objects.filter(mesocycle=meso, deleted_at__isnull=True))
        session = None
        for w in live_weeks:
            new_session = Session.objects.create(week=w, session_slot=slot)
            if w.pk == week.pk:
                session = new_session
        exercise_slot = ExerciseSlot.objects.create(
            session_slot=slot, name="New exercise", order=0
        )
        for w in live_weeks:
            # Blank starter cell (Phase 2a) — see ``_new_block_wide_row``.
            Prescription.objects.create(exercise_slot=exercise_slot, week=w)
        _touch_plan(plan)
    # Row-level reply + refreshed history (see prescription_patch).
    return JsonResponse(
        {
            "ok": True,
            "session": serialize_session(session),
            "history": serialize_plan_history(plan),
        },
        status=201,
    )


@login_required
@require_POST
def session_delete(request, plan_id, pk):
    """Soft-delete one training day — block-wide (P0 fixed-lineup cutover).

    A day's identity is now the ``SessionSlot`` shared across every week, so
    removing it removes the day from the **whole block** (cascading to its
    ``ExerciseSlot``s and every week's ``Session`` instance), not just the
    viewed week (the old per-week semantics). Any ``SessionLog``/``LoggedSet``
    the athlete already logged are untouched — preserving them is the point.
    The row (and its week) must be live, or this 404s. Response is pinned to
    the session's own week.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    session = get_object_or_404(
        Session,
        pk=pk,
        week__mesocycle__plan=plan,
        deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )
    week = session.week
    with transaction.atomic():
        record_plan_action(plan, f"Deleted Day {session.day_number}")
        session.session_slot.soft_delete()
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _parse_id_list(request):
    """A designer reorder POST's ``{"order": [...]}`` body, or a bare 400.

    Structural failures — malformed JSON, a non-object body, a missing/non-list
    ``order``, or a non-int entry — are asserted as a bare 400 (mirrors
    ``prescription_patch``'s ``HttpResponseBadRequest`` convention for
    structurally-invalid bodies), as opposed to the *semantic* id-set mismatch
    a caller checks afterward against the live rows it's reordering (which the
    spec promises as ``{"ok": false, "error": ...}``). ``bool`` is an ``int``
    subclass, so it's excluded explicitly — the same guard ``_body_week_id`` uses.
    """
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return None, HttpResponseBadRequest("Expected a JSON object.")
    order = payload.get("order")
    if not isinstance(order, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in order
    ):
        return None, HttpResponseBadRequest("order must be a list of integers.")
    return order, None


@login_required
@require_POST
def session_reorder(request, plan_id, pk):
    """Reorder one session's exercise rows (dnd-kit designer, Phase 4, #403).

    Body ``{"order": [<cell ids>]}`` must be EXACTLY the viewed week's live row
    cells (``session.cells()``) — one entry per live row, no missing/extra/
    duplicate/foreign/soft-deleted id — in the new order; any mismatch is a 400
    ``{"ok": false, "error": ...}`` (see ``_parse_id_list`` for the structural-
    vs-semantic 400 split). P0 fixed-lineup cutover: row order lives on the
    ``ExerciseSlot`` (block-wide identity), so each posted cell id is mapped to
    its slot and the write reorders the row block-wide, not just this week —
    consistent, since a row's position was always shared block identity, never
    a per-week fact. Writes dense 0-based ``ExerciseSlot.order`` values
    matching the posted order. Idempotent: posting the current order is a 200
    no-op that still records one action (the client never sends a no-op post,
    so this is the simplest contract rather than a special case).
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    session = get_object_or_404(
        Session,
        pk=pk,
        week__mesocycle__plan=plan,
        deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    )
    order, bad = _parse_id_list(request)
    if bad is not None:
        return bad

    week = session.week
    with transaction.atomic():
        # Lock ordering: plan first (see session_add) — the live id set is read
        # under this lock so a concurrent write to the same session's rows
        # can't slip in between the read and this reorder's write.
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        live = list(session.cells())
        live_ids = [c.pk for c in live]
        if len(order) != len(live_ids) or set(order) != set(live_ids):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "order must be exactly the session's exercises.",
                },
                status=400,
            )
        record_plan_action(plan, "Reordered exercises")
        slot_id_by_cell = {c.pk: c.exercise_slot_id for c in live}
        for index, cell_id in enumerate(order):
            ExerciseSlot.objects.filter(pk=slot_id_by_cell[cell_id]).update(order=index)
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


@login_required
@require_GET
def week_view(request, plan_id, week_id):
    """Serialize one week's grid so the designer can switch to it (multi-week).

    A pure read — viewing a week never changes which week is live or what delivery
    targets — so it is scoped by ownership only (404/403), **not** billing-gated:
    an over-limit coach keeps read access to every week. A week that isn't this
    plan's is a flat 404. Returns the same ``serialize_plan`` shape the page hydrates
    from, pinned to ``week`` (``viewing`` reports it back).
    """
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(
        Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    )
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _default_grid_mesocycle(plan):
    """The block that opens/grounds when nothing pins one explicitly.

    Shared by the P1 grid default (§2.7) and the agent's block scope (§4b,
    ``agent_propose``/``_reserve_plan_draft`` when the request carries no
    ``mesocycle_id``): the plan's first block by ``order`` — full stop.
    ``None`` only when the plan has no block at all.

    This is a genuine simplification versus the old ``current_week``-routed
    version, not just a rename: that one preferred the earliest-LIVE-week's
    block, falling back to the first block only when that block had no
    materialized weeks yet. Dropping the live-week detour means the answer no
    longer depends on which weeks happen to be materialized/deleted — "the
    plan's first block" is a single, stable fact, and it's what a coach
    opening a fresh designer/grid/agent run actually expects to land on.
    """
    return plan.mesocycles.order_by("order").first()


@login_required
@require_GET
def api_mesocycle_grid(request, plan_id):
    """The P1 multi-week table's data: every live day × row × week cell.

    A pure read (mirrors ``week_view``) — scoped by ownership only (404/403),
    **not** billing-gated: an over-limit coach keeps read access. Defaults to
    ``_default_grid_mesocycle`` (the plan's earliest-live-week block);
    ``?mesocycle=<id>`` views another block of the same plan (404 for one
    that doesn't belong to it, 400 for a non-integer). A plan with no block at
    all is a 404; a block with no materialized weeks yet returns a valid,
    empty-ish grid.
    """
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    raw_mesocycle_id = request.GET.get("mesocycle")
    if raw_mesocycle_id is not None:
        try:
            mesocycle_id = int(raw_mesocycle_id)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("mesocycle must be an integer.")
        mesocycle = get_object_or_404(Mesocycle, pk=mesocycle_id, plan=plan)
    else:
        mesocycle = _default_grid_mesocycle(plan)
        if mesocycle is None:
            raise Http404("This plan has no block yet.")
    return JsonResponse({"ok": True, **serialize_mesocycle_grid(mesocycle)})


@login_required
@require_POST
def week_add(request, plan_id):
    """Materialize the next week in the plan's active block and open onto it.

    The designer's "+ Add week": grows the block the coach is **viewing** by
    copying its latest week's grid (``Mesocycle.append_week``). The new week is
    live and editable immediately (programs are date-less; there's no "current"
    week it could preempt). Scoped + edit-gated like the other designer writes
    (403 foreign, 402 over-limit). Row-locks the mesocycle so two concurrent
    submits can't both read the same max index and collide on
    ``unique_week_index`` (explicit transaction — prod views run in
    autocommit). Returns the plan pinned to the new week so the client
    switches to it.

    The client posts ``mesocycle_id`` (the grid's open block); the ``plan=plan``
    filter is the security check, so a foreign block 404s. Lacking one, this
    falls back to ``_default_grid_mesocycle`` — the SAME default the grid itself
    opens on. It used to fall back to ``current_week(plan).mesocycle``, the
    earliest *live* week's block, which disagreed with the grid whenever the
    plan's first block had no materialized weeks: the coach saw an empty block 1
    but "+ Add week" appended to block 2, and the refetched grid still showed
    block 1 empty — the week was unreachable.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    # This body now SELECTS THE TARGET BLOCK (mesocycle_id), unlike the old
    # single-block plan — silently downgrading malformed JSON to `{}` (the
    # prior behavior) would add the week to the DEFAULT block instead of the
    # one the coach's client actually meant, and valid-but-non-object JSON
    # (e.g. `[]`) would reach `.get()` below and 500. A bodyless / form /
    # multipart post (no DECLARED JSON body) still means "use the default
    # block" — same `_body_week_id` convention just above (content_type !=
    # application/json, or an empty body, is absence, not an error; `apiPost`
    # always sets the JSON content-type even for `body: null`, which lands
    # empty here). A body that DOES declare itself JSON, though, is validated
    # strictly, same as `agent_propose`: malformed JSON or anything that
    # isn't an object fails loudly (400) before any write, rather than
    # silently landing on the default block or 500ing on `.get()`.
    if request.content_type == "application/json" and request.body:
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Malformed JSON.")
        if not isinstance(payload, dict):
            return HttpResponseBadRequest("Expected a JSON object.")
    else:
        payload = {}
    mesocycle_id = payload.get("mesocycle_id")
    if mesocycle_id is not None:
        try:
            mesocycle_id = int(mesocycle_id)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("mesocycle_id must be an integer.")
        mesocycle = get_object_or_404(Mesocycle, pk=mesocycle_id, plan=plan)
    else:
        mesocycle = _default_grid_mesocycle(plan)
    if mesocycle is None:
        return HttpResponseBadRequest("This plan has no block to add a week to.")
    with transaction.atomic():
        # Lock ordering: plan first (see session_add).
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        Mesocycle.objects.select_for_update().filter(pk=mesocycle.pk).first()
        # Mirrors ``Mesocycle.append_week``'s own indexing (over ALL weeks,
        # deleted included) so the recorded label matches the week it creates —
        # computed under the same lock, so there's no race between the two.
        next_index = (mesocycle.weeks.aggregate(m=Max("index"))["m"] or 0) + 1
        record_plan_action(plan, f"Added Week {next_index}")
        new_week = mesocycle.append_week()
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=new_week)}, status=201)


@login_required
@require_POST
def week_delete(request, plan_id, week_id):
    """Soft-delete one week (designer framework Phase 0, issue #401).

    ``week.soft_delete()`` stamps ``deleted_at`` on the target ``Week`` and
    cascades to its ``Session`` instances (P0 fixed-lineup cutover) — a
    session independently soft-deleted earlier stays deleted if a later undo
    restores this week, since the cascade only ever stamps still-live rows.
    Cells carry no ``deleted_at`` of their own; they're hidden via the join to
    this dead week regardless.

    One rule gates the action itself (400, not the row's own 404 — it exists
    and is live): the plan's **last remaining live week** can't be deleted (a
    plan always needs at least one). Any other live week is deletable —
    programs are date-less and the app no longer tracks a "current" week, so
    there's nothing left to make current first (docs/meso/remove-current-
    week-plan.md §2.8). Row-locks the plan and re-reads the row's liveness
    under that lock, so a concurrent second delete can't race the
    last-live-week count. Response is *not* pinned to a week —
    ``serialize_plan`` falls back to its own default (the plan's earliest live
    week), which the client uses to reopen even if the deleted week was the
    one being viewed.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(
        Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    )
    with transaction.atomic():
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        week.refresh_from_db()
        if week.deleted_at is not None:
            raise Http404("Week not found.")
        live_week_count = Week.objects.filter(
            mesocycle__plan=plan, deleted_at__isnull=True
        ).count()
        if live_week_count <= 1:
            return JsonResponse(
                {"ok": False, "error": "A plan needs at least one week."},
                status=400,
            )
        record_plan_action(plan, f"Deleted Week {week.index}")
        week.soft_delete()
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan)})


@login_required
@require_POST
def week_reorder_sessions(request, plan_id, week_id):
    """Reorder one week's training days (dnd-kit designer, Phase 4, #403).

    Body ``{"order": [<session ids>]}`` must be EXACTLY the week's live session
    id set, in the new order — validation mirrors ``session_reorder`` (see
    ``_parse_id_list`` for the structural-vs-semantic 400 split). P0
    fixed-lineup cutover: a day's order lives on the ``SessionSlot``
    (block-wide identity, ``Session.order`` is now just a delegating
    property), so each posted session id is mapped to its slot and the write
    reorders the day block-wide, not just this week — consistent, since a
    day's position was always shared block identity. Writes dense 0-based
    ``SessionSlot.order`` values only; ``day_number``/``name`` stay untouched —
    "Day 1" keeps its label, since ``order`` is presentation order, not the
    day's identity.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(
        Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    )
    order, bad = _parse_id_list(request)
    if bad is not None:
        return bad

    with transaction.atomic():
        # Lock ordering: plan first (see session_add).
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        live = list(week.sessions.filter(deleted_at__isnull=True))
        live_ids = [s.pk for s in live]
        if len(order) != len(live_ids) or set(order) != set(live_ids):
            return JsonResponse(
                {"ok": False, "error": "order must be exactly the week's days."},
                status=400,
            )
        record_plan_action(plan, "Reordered days")
        slot_id_by_session = {s.pk: s.session_slot_id for s in live}
        for index, session_id in enumerate(order):
            SessionSlot.objects.filter(pk=slot_id_by_session[session_id]).update(
                order=index
            )
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _undo_redo_week_response(plan, week_id):
    """The (week, week) tuple to pin an undo/redo reply to (viewed-week rule).

    The posted ``week_id`` wins when it's still a live week of this plan (the
    coach stays where they were looking); otherwise ``serialize_plan`` falls
    back to the plan's current week — the case where the just-undone/redone
    action itself un-created or re-created the viewed week.
    """
    if week_id is None:
        return None
    return Week.objects.filter(
        pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    ).first()


@login_required
@require_POST
def api_plan_undo(request, plan_id):
    """Pop the plan's most recent undo action and restore it (Phase 1 op-log).

    Every mutating designer endpoint records one ``PlanAction`` — a plan-wide
    snapshot of editable state taken just before its write (``history.py``).
    This pops the max-seq undo row, pushes its mirror-image redo row (same
    seq+label, snapshot = the *current* state, so redo can put it right back),
    and restores the popped snapshot — flipping fields/``deleted_at`` only,
    never hard-deleting or recreating a row, so an undone add redoes onto the
    same pk and an undone delete resurfaces with its athlete's logs untouched.
    Optional JSON body ``{"week_id"}`` pins the reply's viewed week (see
    ``_undo_redo_week_response``). A snapshot referencing a row that no longer
    exists (history rot — soft delete bypassed) is a 409 that rolls back the
    whole attempt, leaving the stacks untouched.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    try:
        with transaction.atomic():
            Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
            popped = (
                PlanAction.objects.filter(plan=plan, stack=PlanAction.Stack.UNDO)
                .order_by("-seq")
                .first()
            )
            if popped is None:
                return JsonResponse(
                    {"ok": False, "error": "Nothing to undo"}, status=400
                )
            redo_snapshot = serialize_plan_snapshot(plan)
            restore_snapshot, seq, label = popped.snapshot, popped.seq, popped.label
            popped.delete()
            PlanAction.objects.create(
                plan=plan,
                stack=PlanAction.Stack.REDO,
                seq=seq,
                label=label,
                snapshot=redo_snapshot,
            )
            restore_plan_snapshot(plan, restore_snapshot)
            _touch_plan(plan)
    except HistoryUnavailable:
        return JsonResponse({"ok": False, "error": "History unavailable"}, status=409)
    week = _undo_redo_week_response(plan, week_id)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


@login_required
@require_POST
def api_plan_redo(request, plan_id):
    """Mirror of ``api_plan_undo``: pop the plan's min-seq redo row and re-apply it."""
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    try:
        with transaction.atomic():
            Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
            popped = (
                PlanAction.objects.filter(plan=plan, stack=PlanAction.Stack.REDO)
                .order_by("seq")
                .first()
            )
            if popped is None:
                return JsonResponse(
                    {"ok": False, "error": "Nothing to redo"}, status=400
                )
            undo_snapshot = serialize_plan_snapshot(plan)
            restore_snapshot, seq, label = popped.snapshot, popped.seq, popped.label
            popped.delete()
            PlanAction.objects.create(
                plan=plan,
                stack=PlanAction.Stack.UNDO,
                seq=seq,
                label=label,
                snapshot=undo_snapshot,
            )
            restore_plan_snapshot(plan, restore_snapshot)
            _touch_plan(plan)
    except HistoryUnavailable:
        return JsonResponse({"ok": False, "error": "History unavailable"}, status=409)
    week = _undo_redo_week_response(plan, week_id)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _live_session_in_plan_or_none(plan, session_id):
    """A live ``Session`` of ``plan`` by pk, or ``None`` (a bad body reference).

    A body-referenced id that
    doesn't resolve to a live row of this plan answers 400, not the URL-segment
    404 used for the endpoint's own ``pk``.
    """
    return Session.objects.filter(
        pk=session_id,
        week__mesocycle__plan=plan,
        deleted_at__isnull=True,
        week__deleted_at__isnull=True,
    ).first()


@login_required
@require_POST
def prescription_move(request, plan_id, pk):
    """Move one exercise row to a different session, within the same week (Phase 4, #403).

    The designer's cross-day drag. P0 fixed-lineup cutover: a row's identity
    is the ``ExerciseSlot`` (block-wide, shared across every week), so this
    re-points the cell's ``exercise_slot.session_slot`` to the target day's
    ``SessionSlot`` — a **block-wide** move, not just this week's — and
    densely renumbers (0-based) BOTH the source and target slot's live
    exercise slots, with the moved row landing at the posted ``index``
    (clamped into ``[0, len(target's live rows)]`` — a drop past either end
    just lands at that end). A target session equal to the source behaves
    like a plain within-day reorder (the row never leaves, only the one day's
    rows are renumbered). Cross-*week* moves — a target session in a
    different week than the source — are a 400
    ``{"ok": false, "error": "Move within one week."}``; the designer's grid
    has no cross-week drag gesture. ``LoggedSet.prescription`` rows are left
    untouched — a move only ever changes the slot's ``session_slot``/``order``,
    never touches an athlete's logged history, so it keeps pointing at the
    same cell pk.

    Body ``{"session_id": <int>, "index": <int>}``; malformed JSON, a non-object
    body, or a missing/non-int field is a bare 400 (mirrors ``prescription_patch``).
    A ``session_id`` that doesn't resolve to a live session of THIS plan is also
    a 400 (``_live_session_in_plan_or_none``'s bad-reference convention) rather
    than a 404 — only the URL-segment ``pk`` gets the 404 treatment.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    session_id = payload.get("session_id")
    if not isinstance(session_id, int) or isinstance(session_id, bool):
        return HttpResponseBadRequest("session_id must be an integer.")
    index = payload.get("index")
    if not isinstance(index, int) or isinstance(index, bool):
        return HttpResponseBadRequest("index must be an integer.")

    target_session = _live_session_in_plan_or_none(plan, session_id)
    if target_session is None:
        return HttpResponseBadRequest("session_id must be a live session of this plan.")
    if target_session.week_id != cell.week_id:
        return JsonResponse({"ok": False, "error": "Move within one week."}, status=400)

    source_session = _session_for_cell(cell)
    week = source_session.week
    source_slot = source_session.session_slot
    target_slot = target_session.session_slot
    with transaction.atomic():
        # Lock ordering: plan first (see session_add).
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        record_plan_action(plan, f"Moved {cell.name or 'exercise'}")
        es = cell.exercise_slot
        if target_slot.pk == source_slot.pk:
            siblings = list(
                ExerciseSlot.objects.filter(
                    session_slot=source_slot, deleted_at__isnull=True
                )
                .exclude(pk=es.pk)
                .order_by("order")
            )
            clamped = max(0, min(index, len(siblings)))
            siblings.insert(clamped, es)
            for new_order, row in enumerate(siblings):
                ExerciseSlot.objects.filter(pk=row.pk).update(order=new_order)
        else:
            target_rows = list(
                ExerciseSlot.objects.filter(
                    session_slot=target_slot, deleted_at__isnull=True
                ).order_by("order")
            )
            clamped = max(0, min(index, len(target_rows)))
            target_rows.insert(clamped, es)
            for new_order, row in enumerate(target_rows):
                ExerciseSlot.objects.filter(pk=row.pk).update(
                    order=new_order, session_slot_id=target_slot.pk
                )
            source_rows = list(
                ExerciseSlot.objects.filter(
                    session_slot=source_slot, deleted_at__isnull=True
                )
                .exclude(pk=es.pk)
                .order_by("order")
            )
            for new_order, row in enumerate(source_rows):
                ExerciseSlot.objects.filter(pk=row.pk).update(order=new_order)
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


def _json_object_body(request):
    """The request's JSON object body, tolerantly parsed (P2 exceptions, #440).

    A real client always posts ``application/json`` — even a bodyless write
    sets an explicit empty/`null` JSON body (mirrors ``apiPost``). A bodyless
    or non-JSON POST (a bare ``client.post(url)`` with no ``data=``, or a
    form/multipart body) carries no JSON payload at all, so it's treated as
    ``{}`` rather than a parse error (same content-type guard as
    ``_body_week_id``) — the multipart test client still sends a *non-empty*
    trailing-boundary body for a bodyless post, so checking ``content_type``
    first (not just "is the body truthy") is required. A *declared* JSON body
    that fails to parse, or isn't an object, is a 400.
    """
    if request.content_type != "application/json" or not request.body:
        return {}, None
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"ok": False, "error": "Malformed JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse(
            {"ok": False, "error": "Expected a JSON object."}, status=400
        )
    return payload, None


def _reap_empty_pending_log(log):
    """Delete ``log`` if it now holds nothing at all. Returns whether it went.

    ``_scroll_hint``, ``_athlete_default_plan_id`` and ``serialize_recent_logs``
    all read ANY ``SessionLog`` as athlete activity, so a log with no sets and no
    notes is not harmless — it keeps moving the athlete's last-trained week and
    polluting recent-log grounding, for work that was mistyped and cleared, or
    never landed at all.

    Only PENDING, only with no notes, only with no remaining sets. A DONE log is
    a finished performance and is never reaped, and neither is one carrying the
    athlete's notes — those hold information even with zero sets.
    """
    if (
        log.status != SessionLog.Status.PENDING
        or (log.notes or "").strip()
        or log.sets.exists()
    ):
        return False
    log.delete()
    return True


@login_required
@require_POST
def prescription_skip(request, plan_id, pk):
    """Toggle a cell's one-week ``skipped`` exception (P2 exceptions, issue #440).

    Body ``{"skipped": <bool>}`` — required; a missing or non-bool value is a
    400 ``{"ok": false, ...}`` rather than a silent no-op. Renders as the
    grid's em-dash cell (``skipped=True``) without touching any other week's
    cell or the block-shared ``ExerciseSlot``.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad

    skipped = payload.get("skipped")
    if not isinstance(skipped, bool):
        return JsonResponse(
            {"ok": False, "error": "skipped must be a boolean."}, status=400
        )

    with transaction.atomic():
        record_plan_action(
            plan, f"Skipped {cell.name}" if skipped else f"Restored {cell.name}"
        )
        cell.skipped = skipped
        cell.save(update_fields=["skipped"])
        # Deliberately does NOT touch already-derived LoggedSets. Skipping a row
        # the athlete has already performed does not un-perform it: this
        # codebase's settled position (see `athlete_log_session`'s delete, which
        # scopes itself to `trainable_cells()` for exactly this reason) is that a
        # set logged against a since-skipped cell is HISTORY, not draft state,
        # and wiping it would silently destroy the athlete's record. A parsed set
        # is no different from a structured one here. What 5a does add is a guard
        # on the CREATE side — `_upsert_parsed_set` won't mint a NEW set for a
        # row that is currently skipped — which is a separate question from
        # preserving one already earned. Leaving them also means unskipping needs
        # no re-derive: nothing was destroyed to restore.
        _touch_plan(plan)
    return JsonResponse({"ok": True, "history": serialize_plan_history(plan)})


@login_required
@require_POST
def cell_line_write(request, plan_id, slot_id):
    """Upsert one freeform (week × line) cell of an exercise row (Phase 2a).

    The sub-line write path (plan §2.3/§2.6): a row's per-week stack is
    sparse, so the client addresses a cell by ``(exercise_slot, week, line)``
    rather than pk — the cell may not exist yet. Body
    ``{"week_id": <int>, "line": <int>, "text": "<str>"}``; the row is
    ``get_or_create``d and its text set (blank text clears the sub-line in
    place — spreadsheet semantics, never a delete). ``line`` 0 is allowed
    (it's just the prescription line, pre-created by every constructive
    write, so the get half hits). Line > ``MAX_CELL_LINE`` is a 400.

    Replaces the retired one-week ``prescription_swap`` endpoint: a
    substitution is typed into a sub-line now, not stored as a field.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    slot = get_object_or_404(
        ExerciseSlot,
        pk=slot_id,
        session_slot__mesocycle__plan=plan,
        deleted_at__isnull=True,
        session_slot__deleted_at__isnull=True,
    )
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad

    week_id = payload.get("week_id")
    if not isinstance(week_id, int) or isinstance(week_id, bool):
        return JsonResponse(
            {"ok": False, "error": "week_id must be an integer."}, status=400
        )
    week = Week.objects.filter(
        pk=week_id,
        mesocycle=slot.session_slot.mesocycle,
        deleted_at__isnull=True,
    ).first()
    if week is None:
        return JsonResponse(
            {"ok": False, "error": "week_id must be a live week of this block."},
            status=400,
        )
    line = payload.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        return JsonResponse(
            {"ok": False, "error": "line must be a non-negative integer."}, status=400
        )
    if line > MAX_CELL_LINE:
        return JsonResponse({"ok": False, "error": "line is too large."}, status=400)
    text = payload.get("text")
    if not isinstance(text, str):
        return JsonResponse(
            {"ok": False, "error": "text must be a string."}, status=400
        )
    if len(text) > PATCHABLE_FIELDS["text"]:
        return JsonResponse({"ok": False, "error": "text is too long."}, status=400)

    with transaction.atomic():
        # LOCK ORDER (#562) — the Plan row before any Prescription of it, per
        # ``docs/meso/decisions.md`` ("Row-lock order"). `record_plan_action`
        # below takes this same lock (a no-op re-acquire once it's held), so
        # this line exists purely to move the acquisition AHEAD of the reclaim
        # write under it: `existing.save(...)` UPDATEs a Prescription row, and
        # taking that before the Plan row made this endpoint the one path that
        # ran Prescription→Plan. Harmless while `athlete_cell_write` also
        # reached a cell before the Plan row; a deadlock the moment that path
        # was corrected to take Plan first (#562), because the cell in question
        # is precisely the athlete-authored one an athlete may be blurring.
        #
        # It changes no WRITE order: the flag flip below still lands before
        # `record_plan_action` snapshots, which is what the next comment is
        # about.
        Plan.objects.select_for_update(no_key=True).filter(pk=plan.pk).first()
        existing = Prescription.objects.filter(
            exercise_slot=slot, week=week, line=line
        ).first()
        if existing is not None and existing.athlete_authored:
            # Reclaim-then-snapshot (Phase 4a review): a coach edit reclaims an
            # athlete-authored cell back into coach history. Persist the flag
            # flip ALONE first, so ``record_plan_action`` snapshots this cell as
            # a coach cell still holding the athlete's original text — a later
            # coach undo then RESTORES that text (as a coach-owned cell) instead
            # of hard-deleting the reclaimed row (which the snapshot, taken while
            # the cell was still athlete-authored, would have omitted entirely).
            existing.athlete_authored = False
            existing.save(update_fields=["athlete_authored"])
        record_plan_action(plan, f"Edited {slot.name or 'exercise'}")
        cell, _created = Prescription.objects.get_or_create(
            exercise_slot=slot, week=week, line=line
        )
        cell.text = text
        # A coach edit reclaims an athlete-authored cell (Phase 4a) back into
        # coach history — from here on it's snapshotted and undoable again.
        cell.athlete_authored = False
        cell.save(update_fields=["text", "athlete_authored"])
        # Deliberately touches NO LoggedSet. A parse-at-commit set (5a) derived
        # from this cell is the athlete's performance, and this edit is
        # undoable — `history.py` keeps SessionLog/LoggedSet/AthleteOneRm out
        # of the plan snapshot precisely so "undo must never touch ... athlete
        # data". Deleting it here (an earlier attempt) lost the record with no
        # way back; detaching it (a later one) made it look like a structured
        # row, so the logger's own delete then wiped it on the next save. The
        # set simply stays as it is: overwriting the text above is enough,
        # because the no-double-display suppression (`parsed_set_is_hidden`)
        # asks whether the source line still SHOWS this performance — it
        # re-parses the cell text, it does NOT read `athlete_authored` — so once
        # the coach's text no longer matches the set, it starts rendering again
        # on its own.
        _touch_plan(plan)
    return JsonResponse(
        {
            "ok": True,
            "cell": {
                "id": cell.pk,
                "exercise_slot_id": slot.pk,
                "week_id": week.pk,
                "line": cell.line,
                "text": cell.text,
            },
            "history": serialize_plan_history(plan),
        }
    )


@login_required
@require_POST
def exercise_slot_patch(request, plan_id, slot_id):
    """Patch a row's per-exercise columns — Tempo / Rest / instructions (D2).

    These are block-wide row attributes (one value across every week), so they
    live on the ``ExerciseSlot``, not a cell. Body: any of
    ``{"tempo", "rest", "note"}`` as strings (see ``SLOT_PATCHABLE_FIELDS``
    caps). Unknown keys are ignored, matching ``prescription_patch``.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    slot = get_object_or_404(
        ExerciseSlot,
        pk=slot_id,
        session_slot__mesocycle__plan=plan,
        deleted_at__isnull=True,
        session_slot__deleted_at__isnull=True,
    )
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad

    updates = {}
    for field, max_length in SLOT_PATCHABLE_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            return JsonResponse(
                {"ok": False, "error": f"{field} must be a string."}, status=400
            )
        if len(value) > max_length:
            return JsonResponse(
                {"ok": False, "error": f"{field} is too long."}, status=400
            )
        updates[field] = value

    if updates:
        with transaction.atomic():
            record_plan_action(plan, f"Edited {slot.name or 'exercise'}")
            for field, value in updates.items():
                setattr(slot, field, value)
            slot.save(update_fields=list(updates))
            _touch_plan(plan)
    return JsonResponse(
        {
            "ok": True,
            "row": {
                "exercise_slot_id": slot.pk,
                "tempo": slot.tempo,
                "rest": slot.rest,
                "note": slot.note,
            },
            "history": serialize_plan_history(plan),
        }
    )


@login_required
@require_POST
def prescription_fill(request, plan_id, pk):
    """Copy a cell's text stack to sibling weeks of the same row (P2, #440).

    Body OPTIONAL ``{"week_ids": [<int>...]}`` — the target weeks; absent or
    empty means every OTHER live week of this cell's ``exercise_slot``. Copies
    the row's whole freeform stack for the source week (line 0 + sub-lines,
    Phase 2a) — never a target's ``skipped``, which stays whatever one-week
    exception it was. A target week's stale higher sub-lines are blanked in
    place (spreadsheet semantics), never deleted.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad

    week_ids = payload.get("week_ids")
    if week_ids is not None:
        if not isinstance(week_ids, list) or not all(
            isinstance(w, int) and not isinstance(w, bool) for w in week_ids
        ):
            return JsonResponse(
                {"ok": False, "error": "week_ids must be a list of integers."},
                status=400,
            )

    target_weeks = Week.objects.filter(
        mesocycle=cell.exercise_slot.session_slot.mesocycle,
        deleted_at__isnull=True,
    ).exclude(pk=cell.week_id)
    if week_ids:
        target_weeks = target_weeks.filter(pk__in=week_ids)
    target_weeks = list(target_weeks)

    source_lines = {
        c.line: c.text
        for c in Prescription.objects.filter(
            exercise_slot_id=cell.exercise_slot_id, week_id=cell.week_id
        )
    }
    max_source_line = max(source_lines) if source_lines else 0

    with transaction.atomic():
        record_plan_action(plan, f"Filled {cell.name} across weeks")
        for week in target_weeks:
            for line, text in source_lines.items():
                target, _created = Prescription.objects.get_or_create(
                    exercise_slot_id=cell.exercise_slot_id, week=week, line=line
                )
                if target.text != text:
                    target.text = text
                    target.save(update_fields=["text"])
            Prescription.objects.filter(
                exercise_slot_id=cell.exercise_slot_id,
                week=week,
                line__gt=max_source_line,
            ).exclude(text="").update(text="")
        _touch_plan(plan)
    return JsonResponse(
        {
            "ok": True,
            "filled": len(target_weeks),
            "history": serialize_plan_history(plan),
        }
    )


@login_required
@require_POST
def coach_set_one_rm(request, plan_id, pk):
    """Set or clear an athlete's 1RM from the designer's %1RM badge (1RM Phase 3).

    The coach-side companion to ``athlete_set_one_rm``: a coach prescribing a
    %1RM target needs the athlete's max for it to mean anything, so they can set
    it here directly — useful before the athlete has ever logged the lift.
    Coach-scoped
    via ``_coach_plan_or_forbidden`` (403); the prescription must belong to the
    plan (404). Body ``{"value": "140"}`` — a blank/absent ``value`` *clears* it
    back to the log-derived estimate. The 1RM is the athlete's own
    (``source=manual``, global across their coaches), persisted through the same
    ``set_manual_one_rm`` the athlete logger uses. Returns ``{one_rm, source}`` so
    the badge repaints.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    # A 1RM belongs to an athlete; a template plan has none (parity plan §3.4).
    if plan.athlete is None:
        return HttpResponseBadRequest("A template plan has no athlete 1RM.")
    prescription = _cell_or_404(plan, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    value, ok = meso_one_rm.clean_manual_value(payload.get("value"))
    if not ok:
        return HttpResponseBadRequest("value must be a positive number or blank.")

    row = meso_one_rm.set_manual_one_rm(plan.athlete, prescription, value, plan.unit)
    return JsonResponse(
        {
            "ok": True,
            "one_rm": presenters._one_rm_label(row),
            "source": row.source if row is not None else "",
        }
    )


@login_required
@require_POST
def plan_deliver(request, plan_id):
    """Deliver a **block**: notify the athlete + snapshot its whole mesocycle.

    2d (parity plan §3.3): the athlete already sees every edit live, so
    delivering doesn't *release* anything — it sends the one-time "your block
    is ready" nudge (email + push) and records history: every live week of the
    target block is stamped ``delivered_at`` (one shared timestamp — the notify
    marker) and gets a ``WeekDelivery`` snapshot (retention; feeds the deliver
    screen's optional what-changed diff and, later, PRs). The target week is
    resolved as before — the ``week_id`` in the body (the multi-week designer's
    "send the week I'm viewing"), else the plan's earliest live week
    (``current_week``'s default — the bare "Deliver" button rarely fires this,
    the designer normally posts an explicit ``week_id``) — and only *selects
    which block* to nudge about. The chosen week must belong to the plan (a
    foreign week is a 404). Re-delivering re-stamps every week and writes
    fresh ``WeekDelivery`` rows.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    # A template plan has no athlete to nudge (parity plan §3.4) — deliver a
    # COPY to clients instead (``plan_batch_deliver`` works from a template).
    if plan.is_template:
        return HttpResponseBadRequest("A template has no athlete to deliver to.")
    # An empty body (the bare deliver button) means "no week_id" — target the
    # live week's block, as before; a present-but-malformed body is a 400, not a
    # silent delivery of the wrong block.
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    if week_id is not None:
        target_week = get_object_or_404(
            Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
        )
    else:
        target_week = current_week(plan)
    if target_week is None:
        return HttpResponseBadRequest("This plan has no week to deliver.")
    block = target_week.mesocycle
    now = timezone.now()
    live_weeks = list(block.weeks.filter(deleted_at__isnull=True))
    for week in live_weeks:
        week.delivered_at = now
        week.save(update_fields=["delivered_at"])
        WeekDelivery.objects.create(
            week=week, delivered_at=now, payload=serialize_week_snapshot(week)
        )
    _touch_plan(plan)
    _notify_athlete_block_delivered(request, plan, block, len(live_weeks))
    # #441 P3-5: the deliver step auto-advances the moment the coach delivers
    # their *own* self-link block — gated on the step's predicate so delivering
    # for another athlete they coach doesn't skip it. A no-op unless parked on
    # deliver.
    meso_tour.advance_self_step_if_complete(request.user, "deliver")
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "mesocycle": {"id": block.pk, "name": block.name},
            "week_count": len(live_weeks),
        },
        status=201,
    )


def _target_week_for_batch_copy(copy, source_block):
    """Map the coach's *viewed* block onto one just-duplicated batch copy.

    remove-current-week-plan.md §2.6 FIX 1: before this branch,
    ``Plan.duplicate_for`` mirrored ``is_current`` onto the copy, so
    ``current_week(copy)`` inherited whichever block the coach had open on the
    source plan. That mirror is gone, so left to its own default
    ``current_week(copy)`` always resolves to the copy's FIRST live block —
    a coach batch-delivering from block 2's deliver screen would silently
    stamp + notify every recipient about block 1 instead, with no visible
    signal.

    ``duplicate_for`` preserves both ``mesocycle.order`` and week ``index``
    verbatim (it deep-copies the whole live tree in ``order``/``index``
    sequence onto fresh rows), so "the copy's block at the same ``order`` as
    the block the coach was viewing" IS that block — just re-homed onto new
    pks. Map by position, never by pk.

    Returns the copy's matching block's first live week, or ``None`` when
    there is no ``source_block`` (no explicit/valid ``week_id`` was posted) or
    the copy has nothing at that ``order`` / that block has no live week of
    its own. The latter shouldn't normally happen — ``duplicate_for`` mirrors
    every block unconditionally — but a concurrent edit to the source plan
    mid-batch is a real, if rare, race; callers degrade to
    ``current_week(copy)`` rather than error.
    """
    if source_block is None:
        return None
    copy_block = copy.mesocycles.filter(order=source_block.order).first()
    return first_live_week(copy_block)


@login_required
@require_POST
def plan_batch_deliver(request, plan_id):
    """Deliver an independent COPY of this plan to several clients at once (2c).

    The replacement for the removed group fan-out (parity plan §3.1, D1):
    instead of one shared program + per-member overrides + live-linked
    materialized snapshots, the coach picks clients on the deliver screen and
    each gets their own ``Plan.duplicate_for`` copy — fully independent and
    live-editable per client from that moment on. Each copy's TARGET block —
    the one the coach was viewing on the source plan's deliver screen, see
    ``_target_week_for_batch_copy`` — is stamped + snapshotted exactly like an
    individual deliver (P3), and each athlete gets the one block-level nudge.

    Form POST from the deliver screen (``relationships`` = checkbox ids, plus
    a hidden ``week_id`` mirroring ``deliver.week_id`` — the block the
    ``?week=``-aware deliver screen is confirming); redirects back with a
    flash. Targets must be *active* athletes of this coach; the plan's own
    athlete and soft-suspended (over-seat-limit, D6) links are silently
    dropped from the selection — the screen never offers them, so their
    presence in the POST is a stale/forged form, not a flow to
    error-message. ``week_id`` gets the same treatment: it must resolve to a
    live week of *this* plan or it's ignored (a foreign/other-plan id is a
    stale/forged form too, never honoured) — see the resolution below. The
    whole fan-out runs in one explicit ``transaction.atomic()``
    (``ATOMIC_REQUESTS`` is inert in this deployment, and a half-delivered
    batch would be worse than a clean retry); notifications ride
    ``transaction.on_commit`` so a rollback never nudges.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden

    # A template has no deliver screen to return to (parity plan §3.4) — send the
    # coach back to their library; a normal plan returns to its deliver screen.
    def _back():
        if plan.is_template:
            return redirect("meso:template_library")
        return redirect("meso:deliver_plan", plan_id=plan.pk)

    if current_week(plan) is None:
        messages.error(request, "This plan has no week to deliver.")
        return _back()
    try:
        picked_ids = [int(raw) for raw in request.POST.getlist("relationships")]
    except (TypeError, ValueError):
        return HttpResponseBadRequest("relationships must be ids.")
    if not picked_ids:
        messages.error(request, "Pick at least one client to deliver a copy to.")
        return _back()
    # The block the coach was viewing on the SOURCE plan (FIX 1) — resolved
    # once, here, and mapped onto each copy inside the loop below. A blank
    # field (no hidden input rendered, or an older cached form) means "no
    # opinion" and degrades silently, same as a picked relationship id that
    # doesn't resolve; only a malformed (non-integer) value is a 400, mirroring
    # the ``relationships`` parsing just above.
    raw_week_id = request.POST.get("week_id", "").strip()
    source_block = None
    if raw_week_id:
        try:
            source_week_id = int(raw_week_id)
        except ValueError:
            return HttpResponseBadRequest("week_id must be an id.")
        source_week = Week.objects.filter(
            pk=source_week_id, mesocycle__plan=plan, deleted_at__isnull=True
        ).first()
        source_block = source_week.mesocycle if source_week else None
    targets = list(
        CoachAthlete.objects.for_coach(request.user)
        .active()
        .filter(pk__in=picked_ids)
        .exclude(pk=plan.relationship_id)
        .exclude(pk__in=billing_access.suspended_athlete_ids(request.user))
        .select_related("athlete")
        .order_by("athlete__name", "athlete__email")
    )
    if not targets:
        messages.error(request, "No deliverable clients in that selection.")
        return _back()
    delivered_names = []
    with transaction.atomic():
        # LOCK ORDER (#596) — reserve every target CoachAthlete row in
        # ascending pk before inserting a Plan beneath it. Re-qualify under
        # the lock so a link ended or suspended while the form was open is
        # dropped rather than receiving a new plan mid-cascade.
        locked_target_pks = list(
            CoachAthlete.objects.select_for_update(no_key=True)
            .for_coach(request.user)
            .active()
            .filter(pk__in=[relationship.pk for relationship in targets])
            .exclude(pk__in=billing_access.suspended_athlete_ids(request.user))
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        targets = list(
            CoachAthlete.objects.filter(pk__in=locked_target_pks)
            .select_related("athlete")
            .order_by("athlete__name", "athlete__email")
        )
        if not targets:
            messages.error(request, "No deliverable clients in that selection.")
            return _back()
        for relationship in targets:
            copy = plan.duplicate_for(relationship, status=Plan.Status.ACTIVE)
            target_week = _target_week_for_batch_copy(
                copy, source_block
            ) or current_week(copy)
            block = target_week.mesocycle
            now = timezone.now()
            live_weeks = list(block.weeks.filter(deleted_at__isnull=True))
            for week in live_weeks:
                week.delivered_at = now
                week.save(update_fields=["delivered_at"])
                WeekDelivery.objects.create(
                    week=week,
                    delivered_at=now,
                    payload=serialize_week_snapshot(week),
                )
            _notify_athlete_block_delivered(
                request, copy, block, len(live_weeks), via="batch"
            )
            delivered_names.append(relationship.athlete.display_name())
    messages.success(
        request,
        f"Delivered an independent copy to {', '.join(delivered_names)}.",
    )
    return _back()


@login_required
@require_POST
def template_use(request, plan_id):
    """Start-for-client — deep-copy a template into a fresh client plan (§3.4).

    The working-copy door of the template library: the owner picks one active
    client and the template is deep-copied (``duplicate_for``) into a live,
    ACTIVE, *undelivered* plan for that relationship, then opened in the
    designer. Nothing is stamped or notified — the copy is live per the 2d
    model (the athlete sees it), but no delivery snapshot is written and no
    nudge is sent; batch-deliver is the separate "notify" door.

    Only serves templates the requester owns (``editable_by`` + ``is_template``
    → 404 for a foreign or non-template plan). A missing / non-numeric / foreign
    ``relationship`` creates nothing and flashes back to the library — as does a
    soft-suspended (over-seat-limit, D6) link, which the library never offers, so
    its presence in a POST is a stale/forged form; the suspended-id exclusion
    mirrors ``plan_batch_deliver`` so a frozen client can't be started. The copy
    is written in one explicit ``transaction.atomic()`` (``ATOMIC_REQUESTS`` is
    inert here).
    """
    plan = get_object_or_404(
        Plan.objects.editable_by(request.user).filter(is_template=True),
        pk=plan_id,
    )
    try:
        rel_id = int(request.POST.get("relationship", ""))
    except (TypeError, ValueError):
        rel_id = None
    relationship = None
    if rel_id is not None:
        relationship = (
            CoachAthlete.objects.for_coach(request.user)
            .active()
            .filter(pk=rel_id)
            .exclude(pk__in=billing_access.suspended_athlete_ids(request.user))
            .select_related("athlete")
            .first()
        )
    if relationship is None:
        messages.error(
            request, "Pick one of your active clients to start this template."
        )
        return redirect("meso:template_library")
    with transaction.atomic():
        # LOCK ORDER (#596) — the link is the parent of the Plan about to be
        # inserted. Re-read every eligibility predicate while holding its
        # no-key row lock so a concurrent cascade/closure wins cleanly.
        relationship = (
            CoachAthlete.objects.select_for_update(no_key=True)
            .for_coach(request.user)
            .active()
            .filter(pk=rel_id)
            .exclude(pk__in=billing_access.suspended_athlete_ids(request.user))
            .select_related("athlete")
            .first()
        )
        if relationship is None:
            messages.error(
                request, "Pick one of your active clients to start this template."
            )
            return redirect("meso:template_library")
        copy = plan.duplicate_for(relationship, status=Plan.Status.ACTIVE)
    track(
        EventName.TEMPLATE_IMPORTED,
        actor=request.user,
        subject=copy,
        template=plan.pk,
        athlete=str(relationship.athlete_id),
        demo=relationship.is_demo,
    )
    messages.success(
        request,
        f"Started {copy.title} for {relationship.athlete.display_name()}.",
    )
    return redirect("meso:designer_plan", plan_id=copy.pk)


def _notify_athlete_block_delivered(
    request, plan, mesocycle, week_count, *, via="deliver"
):
    """Best-effort: ONE email + ONE push that a whole **block** was delivered.

    The deliver nudge (P3; per-week notification retired with the 2d live+notify
    model): the deliver path nudges about the whole mesocycle at once, so the
    athlete gets a single "your new block is ready" heads-up — not one
    notification per week. Sandbox-gated at the coach check, deferred
    to ``transaction.on_commit`` (under ``ATOMIC_REQUESTS`` a rolled-back deliver
    must not notify a false "your block is ready"), and each channel is
    independently best-effort — a failure in one is swallowed and logged, never a
    500 or a rolled-back deliver, and never blocks the other.

    Also the ``block_delivered`` analytics choke point (#509) for both callers
    (``plan_deliver`` via ``via="deliver"``, ``plan_batch_deliver`` via
    ``via="batch"``) — tracked synchronously here, not inside ``_send``, which
    only runs once the transaction actually commits.

    Sandbox gate (S4): a sandbox coach's deliveries never notify — there is no
    real person behind a seeded demo athlete.
    """
    if meso_sandbox.is_sandbox(plan.coach):
        return
    track(
        EventName.BLOCK_DELIVERED,
        actor=request.user,
        subject=mesocycle,
        plan=plan.pk,
        athlete=str(plan.athlete.pk),
        weeks=week_count,
        via=via,
        demo=plan.is_demo,
    )
    home_url = request.build_absolute_uri(reverse("meso:athlete_home"))
    unsubscribe_url = request.build_absolute_uri(
        reverse(
            "meso:unsubscribe_delivery_email",
            kwargs={"token": make_unsubscribe_token(plan.athlete)},
        )
    )

    def _send():
        try:
            # The athlete can opt out of delivery emails (the email's
            # List-Unsubscribe link). Push is a separate, browser-opt-in channel
            # and is never gated by the email opt-out.
            if not athlete_opted_out(plan.athlete):
                send_block_delivered_email(
                    athlete=plan.athlete,
                    coach=plan.coach,
                    plan=plan,
                    week_count=week_count,
                    home_url=home_url,
                    unsubscribe_url=unsubscribe_url,
                )
        except Exception:  # mail is best-effort; never fail a delivery on it
            logger.exception(
                "Failed to send block delivery email for plan %s mesocycle %s",
                plan.pk,
                mesocycle.pk,
            )
        try:
            meso_push.notify_block_delivered(
                athlete=plan.athlete,
                coach=plan.coach,
                plan=plan,
                mesocycle=mesocycle,
                week_count=week_count,
                home_url=home_url,
            )
        except Exception:  # push is best-effort too; never fail a delivery on it
            logger.exception(
                "Failed to send block delivery push for plan %s mesocycle %s",
                plan.pk,
                mesocycle.pk,
            )

    transaction.on_commit(_send)


@csrf_exempt
def unsubscribe_delivery_email(request, token):
    """Login-free, tokened opt-out from training-delivery emails.

    Reached from the delivery email's ``List-Unsubscribe`` link. A mail client
    honoring RFC 8058 one-click POSTs here directly (``List-Unsubscribe=
    One-Click``, no CSRF token — hence ``@csrf_exempt``); a human who clicks the
    visible footer link lands on a GET confirm page and POSTs the form. We never
    mutate on GET: mail scanners and link prefetchers issue GETs and must not
    silently unsubscribe anyone. The signed token authorizes — no login needed
    (the recipient may not be signed in, or signed in under a different address).
    """
    user = resolve_unsubscribe_user(token)
    if user is None:
        return render(request, "meso/unsubscribe_invalid.html", status=400)
    if request.method == "POST":
        set_delivery_email_opt_out(user, True)
        return render(request, "meso/unsubscribe_done.html", {"email": user.email})
    return render(request, "meso/unsubscribe_confirm.html", {"email": user.email})


# -- agent proposal engine (agent slice Phase 1 / Phase 4 — B6) -----------
#
# Runs the Claude proposal engine for an owned plan and persists a reviewable
# batch (the coach still approves at the review gate). Phase 4 runs it off the
# request thread: the endpoint creates a ``drafting`` batch, dispatches the job,
# and returns 202 + a ``status_url``; the frontend polls ``batch_status`` until
# the batch resolves to ``pending`` (changes + review link) or ``failed`` (with
# the reason). Returns 503 — before creating a batch — when no API key is
# configured, so the feature degrades cleanly in envs without creds.

MAX_INSTRUCTION_LENGTH = 2000


@login_required
@require_POST
def agent_propose(request, plan_id):
    """Kick off an agent run for a plan and return a drafting batch to poll."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    # The agent grounds on the plan's athlete (contraindications, logs); a
    # template plan has none (parity plan §3.4), so there's nothing to ground
    # on — refuse cleanly rather than crash building the context.
    if plan.is_template:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "The agent needs an athlete to program for — "
                    "open a client's plan to use it."
                ),
            },
            status=400,
        )
    # Sandbox gate (S3): the sandbox never calls Anthropic — the agent is the
    # one capability held back, gated behind creating a real account. Checked
    # before any metering/API-key work so a throwaway visitor never reserves a
    # run or touches the client.
    if meso_sandbox.is_sandbox(request.user):
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Create a free account and start a "
                    f"{CoachSubscription.TRIAL_DAYS}-day free trial to run "
                    "the AI agent."
                ),
                "signup_required": True,
                "signup_url": reverse("meso:sandbox_signup"),
            },
            status=403,
        )
    # Reserve the run atomically (S6 Phase 5 metering): lock the coach row, check
    # the allowance, and create the batch in one transaction so concurrent
    # agent_propose calls serialize — the lock is held until the batch row commits,
    # so a second request blocks and then re-counts against the cap. The
    # transaction must be *explicit*: this project does not set
    # ``DATABASES["default"]["ATOMIC_REQUESTS"]`` (see the note in
    # ``config/settings/base.py`` for why it must stay that way), so without
    # this ``select_for_update`` would raise in autocommit
    # on Postgres and the count-then-create gate would be racy. (On SQLite/tests the
    # lock is a no-op; the real serialization is on Postgres in prod.) Early returns
    # below just commit an empty transaction — nothing is written on those paths.
    with transaction.atomic():
        User.objects.select_for_update(no_key=True).filter(pk=request.user.pk).first()
        # Agent gate (D4, flat plan D14): the Claude agent has real per-call cost,
        # so every tier is metered per month except comped. Over the cap → 402 (the
        # designer shows the CTA in place of the composer once exhausted). A *free*
        # coach can upgrade; a *paid* coach has hit their plan cap and just waits for
        # the monthly reset (no higher tier to sell). Defended here, not just in the
        # UI, because the API cost is real.
        if not billing_access.can_use_agent(request.user):
            cap = billing_access.agent_allowance(request.user)
            if billing_access.is_active(request.user):
                error = (
                    f"You've used all {cap} agent runs this month. "
                    "Your allowance resets on the 1st."
                )
                upgrade = False
            else:
                error = (
                    f"You've used all {cap} free agent runs this month. "
                    "Start your free trial or subscribe for more."
                )
                upgrade = True
            return JsonResponse(
                {"ok": False, "error": error, "upgrade": upgrade},
                status=402,
            )
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return HttpResponseBadRequest("Malformed JSON.")
        if not isinstance(payload, dict):
            return HttpResponseBadRequest("Expected a JSON object.")
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return HttpResponseBadRequest("An instruction is required.")
        instruction = instruction.strip()
        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            return HttpResponseBadRequest("Instruction is too long.")

        # §4b (docs/meso/remove-current-week-plan.md): capture which block the
        # coach had open, ONCE, right now, and freeze it onto the batch —
        # grounding runs in a background job and apply on a later request, so
        # neither can re-read a live "current" pointer without risking a
        # different (or, post-``is_current``, silently wrong-block) answer each
        # time. ``plan=plan`` in the lookup IS the security check: a block from
        # a foreign plan 404s exactly like a foreign plan id would, never
        # leaking whether it exists. A request without ``mesocycle_id`` (a
        # legacy or non-designer client) falls back to the plan's first block,
        # the same default the grid uses.
        mesocycle_id = payload.get("mesocycle_id")
        if mesocycle_id is not None:
            try:
                mesocycle_id = int(mesocycle_id)
            except (TypeError, ValueError):
                return HttpResponseBadRequest("mesocycle_id must be an integer.")
            mesocycle = get_object_or_404(Mesocycle, pk=mesocycle_id, plan=plan)
        else:
            mesocycle = _default_grid_mesocycle(plan)
        if mesocycle is None:
            return HttpResponseBadRequest("This plan has no block to program yet.")

        # Guard on the key here so we answer 503 without persisting a dead batch.
        if agent_client.get_default_client() is None:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "The Meso agent is not configured (no API key).",
                },
                status=503,
            )

        batch = agent_service.create_drafting_batch(
            plan,
            instruction,
            coach=request.user,
            mesocycle=mesocycle,
            trigger=AgentProposalBatch.Trigger.MANUAL,
        )
    # The batch is committed; enqueue the worker run and bump the plan outside the
    # lock so neither holds the coach row.
    agent_jobs.dispatch_proposal(batch.pk)
    _touch_plan(plan)
    track(
        EventName.AGENT_PROPOSAL_RUN,
        actor=request.user,
        subject=batch,
        trigger=batch.trigger,
        demo=plan.is_demo,
    )
    return JsonResponse(
        {
            "ok": True,
            "batch_id": batch.pk,
            "status": batch.status,
            "status_url": reverse(
                "meso:api_batch_status", kwargs={"batch_id": batch.pk}
            ),
        },
        status=202,
    )


@login_required
@require_GET
def batch_status(request, batch_id):
    """Poll a proposal batch's state while/after the background job runs.

    Scoped to a batch the requester coaches (404 otherwise). ``drafting`` while
    the job runs; ``pending`` with the serialized changes + a review link once it
    lands; ``failed`` with the reason when the provider/run failed.
    """
    batch = _coach_batch_or_404(request, batch_id)
    data = {"ok": True, "status": batch.status, "summary": batch.summary}
    if batch.status == AgentProposalBatch.Status.FAILED:
        data["error"] = batch.error
    elif batch.status != AgentProposalBatch.Status.DRAFTING:
        changes = [serialize_proposed_change(c) for c in batch.changes.all()]
        data["changes"] = changes
        if changes:
            data["review_url"] = reverse(
                "meso:review_batch", kwargs={"batch_id": batch.pk}
            )
    return JsonResponse(data)


# -- review gate: approve/reject + apply (agent slice Phase 2 — B6) --------
#
# The human gate is the review screen; these endpoints persist the coach's
# per-change decisions and then write the approved edits back into the program.
# Every action is scoped to a batch the requester coaches over an *active*
# relationship (``Plan.objects.for_coach``) — a foreign/unknown batch is a 404,
# never a silent write. Apply/dismiss only act on a still-``pending`` batch, so a
# double-submit is a clean 409 rather than a re-apply.


def _coach_batch_or_404(request, batch_id):
    """The batch the requester coaches, or raise ``Http404``.

    Scoped to a plan the coach may *edit* (``editable_by``) — an individual plan
    over an active relationship; a foreign/unknown batch is a 404.
    """
    batch = (
        AgentProposalBatch.objects.filter(
            pk=batch_id, plan__in=Plan.objects.editable_by(request.user)
        )
        .select_related("plan", "plan__relationship")
        .first()
    )
    if batch is None:
        raise Http404("Unknown proposal batch")
    return batch


@login_required
@require_POST
def change_set_status(request, pk):
    """Persist a coach's approve/reject decision on one proposed change."""
    change = (
        ProposedChange.objects.filter(
            pk=pk, batch__plan__in=Plan.objects.editable_by(request.user)
        )
        .select_related("batch")
        .first()
    )
    if change is None:
        raise Http404("Unknown proposed change")
    if change.batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")
    status = payload.get("status")
    allowed = {ProposedChange.Status.APPROVED, ProposedChange.Status.REJECTED}
    if status not in allowed:
        return HttpResponseBadRequest("status must be 'approved' or 'rejected'.")
    # The pre-check above is unlocked, so this can land after a concurrent
    # ``batch_apply`` commits (#540) — lock the batch and re-check before
    # writing, so a reject can't mark a change REJECTED after the batch that
    # applied it.
    #
    # LOCK ORDER (#559): batch, then its ``ProposedChange`` — no ``Plan`` lock,
    # deliberately. This endpoint never touches the Plan row, and the app-wide
    # order (``docs/meso/decisions.md``) forbids taking two locks out of
    # sequence, not skipping a level. So it stays consistent with both
    # ``batch_apply`` (Plan -> batch -> children) and ``demo.clear_demo``
    # (Plan -> batches -> cascade), and a "Remove demo data" that already holds
    # this batch simply makes this request wait and then answer 404 for a row
    # that is now gone, rather than deadlock with it.
    with transaction.atomic():
        batch = (
            AgentProposalBatch.objects.select_for_update()
            .filter(pk=change.batch_id)
            .first()
        )
        if batch is None:
            raise Http404("Unknown proposal batch")
        if batch.status != AgentProposalBatch.Status.PENDING:
            return JsonResponse(
                {"ok": False, "error": "This batch has already been resolved."},
                status=409,
            )
        change.status = status
        change.save(update_fields=["status"])
    return JsonResponse({"ok": True, "id": change.pk, "status": change.status})


@login_required
@require_POST
def batch_apply(request, batch_id):
    """Apply the batch's approved changes back into the program."""
    batch = _coach_batch_or_404(request, batch_id)
    # Applying a batch writes the approved edits into the program — an edit, so it
    # respects the D6 over-limit freeze (a batch drafted before a downgrade can't be
    # applied while its athlete's link is soft-suspended). Per-plan (S6 Phase 5), so
    # a batch for a kept athlete still applies while the coach is over the cap.
    if not billing_access.can_edit_plan(batch.plan):
        return _over_limit_json()
    if batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    # ONE undo action for the whole batch, snapshotted before any of its
    # changes land — undo reverts every change the batch applied in one step.
    with transaction.atomic():
        # The pre-check above is unlocked, so two concurrent Applies can both
        # pass it (#540). Lock the batch row and re-check, so only the winner
        # records the undo action and applies. (No ``select_related``: it would
        # lock the joined rows too.)
        #
        # LOCK ORDER (#559) — the Plan row FIRST, then the batch. This used to
        # read the other way round ("the batch, then the Plan row
        # ``record_plan_action`` locks; nothing locks them the other way
        # round"), which was true until ``demo.clear_demo`` started locking a
        # plan and then its batches ahead of its cascade delete. That made this
        # endpoint the inversion: an Apply holding the batch and waiting on the
        # Plan, against a "Remove demo data" holding the Plan and waiting on
        # the batch. Plan-before-child is the app-wide order
        # (``docs/meso/decisions.md``), and this path took the Plan lock a few
        # statements later anyway (via ``record_plan_action``), so hoisting it
        # costs nothing: that call now re-acquires a lock already held.
        Plan.objects.select_for_update(no_key=True).filter(pk=batch.plan_id).first()
        batch = (
            AgentProposalBatch.objects.select_for_update().filter(pk=batch.pk).first()
        )
        if batch is None:  # its plan was deleted since the lookup above
            raise Http404("Unknown proposal batch")
        if batch.status != AgentProposalBatch.Status.PENDING:
            return JsonResponse(
                {"ok": False, "error": "This batch has already been resolved."},
                status=409,
            )
        record_plan_action(batch.plan, "Applied agent changes")
        result = agent_apply.apply_batch(batch)
    track(
        EventName.BATCH_APPLIED,
        actor=request.user,
        subject=batch,
        applied=result["applied"],
        skipped=result["skipped"],
        demo=batch.plan.is_demo,
    )
    # Where the review screen sends the coach next: the deliver screen, pinned to
    # the block the batch actually edited. A bare deliver URL resolves its own
    # week via ``current_week(plan)`` — the plan's earliest live week — so a coach
    # who ran the agent on block 2 would land on block 1 and see none of the
    # changes they just applied. ``?week=`` is the deliver screen's own selector.
    # Falls back to the bare URL when the batch has no block (a legacy row, or one
    # hard-deleted since — ``SET_NULL``) or its block has no live weeks.
    next_url = reverse("meso:deliver_plan", kwargs={"plan_id": batch.plan_id})
    applied_week = first_live_week(batch.mesocycle)
    if applied_week is not None:
        next_url = f"{next_url}?week={applied_week.pk}"
    return JsonResponse(
        {
            "ok": True,
            "applied": result["applied"],
            "skipped": result["skipped"],
            "deliver_url": next_url,
        }
    )


@login_required
@require_POST
def batch_dismiss(request, batch_id):
    """Discard a batch without applying anything."""
    batch = _coach_batch_or_404(request, batch_id)
    if batch.status != AgentProposalBatch.Status.PENDING:
        return JsonResponse(
            {"ok": False, "error": "This batch has already been resolved."}, status=409
        )
    with transaction.atomic():
        # The same lock and re-check as ``batch_apply`` (#540), so a Dismiss
        # racing an Apply can't mark the applied batch DISMISSED.
        batch = (
            AgentProposalBatch.objects.select_for_update().filter(pk=batch.pk).first()
        )
        if batch is None:
            raise Http404("Unknown proposal batch")
        if batch.status != AgentProposalBatch.Status.PENDING:
            return JsonResponse(
                {"ok": False, "error": "This batch has already been resolved."},
                status=409,
            )
        agent_apply.dismiss_batch(batch)
    return JsonResponse(
        {
            "ok": True,
            "designer_url": reverse(
                "meso:designer_plan", kwargs={"plan_id": batch.plan_id}
            ),
        }
    )


# -- billing (S6 — multi-coach SaaS) ---------------------------------------
#
# A coach subscribes (per-seat, monthly) via a Stripe subscription Checkout
# Session and manages the subscription (card / cancel / invoices) in Stripe's
# hosted Customer Portal. State flows back through ``billing_webhook`` →
# ``billing.webhooks`` into the local ``CoachSubscription`` mirror. The paywall /
# upgrade UI + enforcement choke points land in Phase 3; these are the plumbing.

#: Session keys behind the "Finishing your subscription…" placeholder
#: (adversarial review of #556). The bare ``?billing=success`` query param
#: Checkout's ``success_url`` carries can't tell a just-completed Checkout
#: apart from a stale bookmark, browser history, or a typed URL — any of
#: which would otherwise show the placeholder forever, since no webhook is
#: actually coming. ``billing_subscribe`` sets the *started* marker right
#: before it sends the coach to a real Checkout (or the *pending* marker
#: directly when Stripe itself reports an existing subscription);
#: ``_checkout_pending`` is the only place either is read.
#:
#: The *started* marker is a ``{"id": <Checkout Session id>, "at": <unix ts>}``
#: dict (round 2 of the adversarial review, Fix B) — not just a timestamp — so
#: ``_checkout_pending`` can ask Stripe whether THAT session actually
#: completed before trusting ``?billing=success``. A bare ``?billing=success``
#: is reachable from an abandoned Checkout too (the coach never left the tab,
#: or came back to a stale bookmark): the old bare-timestamp marker had no way
#: to tell that apart from a real completion, so it showed the placeholder
#: over the coach's real state — Subscribe hidden — for up to 15 minutes,
#: and starting the free local trial in between didn't clear it either.
CHECKOUT_STARTED_SESSION_KEY = "meso_checkout_started_at"
CHECKOUT_PENDING_SESSION_KEY = "meso_checkout_pending_at"
#: How long a real Checkout redirect stays eligible to convert into the
#: pending state once ``?billing=success`` comes back — comfortably longer
#: than paying actually takes, short enough that a days-old abandoned tab
#: can't resurrect it.
CHECKOUT_STARTED_MAX_AGE = datetime.timedelta(hours=2)
#: How long the "Finishing…" placeholder itself is shown before giving up on
#: the webhook and falling back to the real (still free/trial) state.
CHECKOUT_PENDING_MAX_AGE = datetime.timedelta(minutes=15)

#: One accurate message for every "couldn't reach Stripe, so we didn't touch
#: Checkout" bounce in ``billing_subscribe``. The old wording promised
#: "nothing was charged" — not something this code can vouch for, since a
#: concurrent tab may have completed a Checkout of its own while this
#: particular call to Stripe failed.
STRIPE_UNAVAILABLE_MESSAGE = (
    "We couldn't reach Stripe just now, so we didn't open checkout. Try "
    "again in a minute."
)


def _checkout_pending(request):
    """Should the billing surfaces show the "Finishing…" placeholder right now?

    Driven by the coach's own session, not the bare ``?billing=success`` query
    param alone (adversarial review of #556): that param can't tell a
    just-completed Checkout apart from a stale bookmark, browser history, a
    typed URL, or an abandoned tab that's simply still sitting on the success
    URL — and it disappears the instant the coach clicks the nav's plain
    "Billing" link, which would otherwise contradict whatever the Checkout
    redirect just showed. ``billing_subscribe`` sets
    ``CHECKOUT_STARTED_SESSION_KEY`` right before redirecting to a real
    Checkout, carrying that Checkout Session's own id; an incoming
    ``?billing=success`` while that marker is fresh asks Stripe whether THAT
    session actually completed (round 2 of the adversarial review, Fix B)
    before converting it into the pending state — a query param alone (no
    Checkout ever started in this session, or one that never finished) does
    nothing. ``?billing=cancel`` clears both markers — the coach abandoned
    Checkout, so there's nothing to finish.

    The started→pending conversion:

    - Stripe says the session is ``complete`` → the pending marker is set and
      the started one dropped.
    - Stripe says it isn't (``open``/``expired`` — abandoned, or its own TTL
      passed) → the started marker is dropped and this returns the real
      state. Starting the free local trial in between (the review's own
      counterexample) is unaffected either way, since it never touches either
      marker.
    - The Stripe lookup itself raises → transient; the started marker is left
      alone (so a reload can try again) and this returns the real state for
      now, never a placeholder it can't back.
    - A legacy bare-float marker (pre-Fix-B, from a coach mid-Checkout across
      the deploy that shipped this) carries no session id to verify — treated
      as if there's no started marker at all rather than trusted blind.

    Once converted, the pending marker persists across requests with no query
    string at all (until it ages out, ``CHECKOUT_PENDING_MAX_AGE``), so the
    roster and the billing page agree regardless of which one the Checkout
    redirect landed on. ``billing_state`` ANDs this with "the mirror has no
    live Stripe subscription yet", so the placeholder disappears the moment
    the webhook actually lands, whatever is still sitting in the session.
    """
    now = timezone.now().timestamp()
    started = request.session.get(CHECKOUT_STARTED_SESSION_KEY)
    if isinstance(started, dict):
        started_at = started.get("at")
        started_session_id = started.get("id")
    elif started is not None:
        # Legacy bare-float marker — no session id to verify against.
        started_at = started
        started_session_id = None
    else:
        started_at = None
        started_session_id = None
    if (
        request.GET.get("billing") == "success"
        and started_session_id is not None
        and started_at is not None
        and now - started_at < CHECKOUT_STARTED_MAX_AGE.total_seconds()
    ):
        try:
            complete = billing_gateway.checkout_session_is_complete(started_session_id)
        except Exception:  # noqa: BLE001 — transient; try again on reload
            logger.exception(
                "Stripe Checkout Session status check failed for %s (coach %s)",
                started_session_id,
                request.user.pk,
            )
        else:
            if complete:
                request.session[CHECKOUT_PENDING_SESSION_KEY] = now
                request.session.pop(CHECKOUT_STARTED_SESSION_KEY, None)
            else:
                # Not complete: abandoned or expired. Nothing to finish.
                request.session.pop(CHECKOUT_STARTED_SESSION_KEY, None)
    if request.GET.get("billing") == "cancel":
        request.session.pop(CHECKOUT_STARTED_SESSION_KEY, None)
        request.session.pop(CHECKOUT_PENDING_SESSION_KEY, None)
        return False
    pending_at = request.session.get(CHECKOUT_PENDING_SESSION_KEY)
    if pending_at is None:
        return False
    if now - pending_at < CHECKOUT_PENDING_MAX_AGE.total_seconds():
        return True
    request.session.pop(CHECKOUT_PENDING_SESSION_KEY, None)
    return False


@login_required
@require_POST
def billing_subscribe(request):
    """Start a subscription Checkout — redirect the coach to Stripe to pay."""
    if not _is_coach(request.user):
        return redirect("meso:roster")
    # Sandbox gate (S4): there's no real coach behind a throwaway sandbox
    # account to bill — never open a Checkout session for one.
    if meso_sandbox.is_sandbox(request.user):
        messages.info(request, "Billing is disabled in the demo.")
        return redirect("meso:roster")
    # The flat Pro plan (D14) needs its one Price configured; ship dormant (bounce
    # gracefully) until the owner creates it, so a deploy never opens a broken Checkout.
    if not settings.MESO_PRO_PRICE_ID:
        messages.error(request, "Subscriptions aren't configured yet.")
        return redirect("meso:roster")
    # Cheap early exits off the coach's possibly-stale, already-loaded mirror
    # (adversarial review, round 2 of #556, Fix A) — a comped coach or one who
    # already has a live Stripe subscription is blocked either way, so there's
    # no reason to pay for a Stripe customer lookup first. Both are
    # re-checked authoritatively under the lock below, off a freshly re-read
    # row, so a stale read here can only cost an unnecessary bounce-and-retry,
    # never a wrong Checkout.
    precheck_sub = getattr(request.user, "coach_subscription", None)
    if precheck_sub and precheck_sub.status == CoachSubscription.Status.COMPED:
        messages.info(request, "Your plan changed. Nothing was charged.")
        return redirect("meso:billing")
    if precheck_sub and precheck_sub.has_live_stripe_subscription:
        messages.info(
            request,
            "You already have a subscription. Manage it in Manage billing.",
        )
        return redirect("meso:billing")
    # The Stripe customer must exist BEFORE the lock below, not inside it
    # (adversarial review, round 2 of #556, Fix A): the atomic block holds the
    # coach's row lock across several Stripe calls (the open-subscription
    # check, expiring other Checkouts, creating the new one), and
    # stripe-python's own read timeout on those (80s) outlives gunicorn's
    # default worker timeout (30s) — if the worker died mid-transaction, the
    # customer id write would roll back with it, orphaning the customer
    # Stripe already has. Creating the customer here, in autocommit, makes
    # the id durable the instant it's written, independent of anything that
    # happens later in this request. This is still safe under a race: two
    # concurrent first-time requests both reaching this line converge on ONE
    # customer because ``stripe_customer_get_or_create`` writes the id
    # write-once (see ``payments/utils.py``) — the loser here simply reads
    # back the winner's id instead of racing it for real.
    try:
        billing_gateway.ensure_customer(request.user)
    except Exception:  # noqa: BLE001 — fail closed, never silently charge
        logger.exception("Stripe customer lookup failed for coach %s", request.user.pk)
        messages.error(request, STRIPE_UNAVAILABLE_MESSAGE)
        return redirect("meso:billing")
    # Two concurrent Subscribe POSTs for one coach (two tabs, or a double
    # submit) must not both pass the checks below and both create a Checkout
    # Session — completing both would double-bill. The mirror can also lag
    # the webhook by a few seconds, long enough for a double-click or an
    # older Checkout tab to slip a second subscription past a mirror-only
    # guard. Lock the coach's user row — ``no_key=True`` for the same reason
    # ``billing.webhooks._lock_mirror`` / ``CoachSubscription.start_trial_for``
    # need it (a plain ``FOR UPDATE`` would deadlock against the commit-time
    # ``FOR KEY SHARE`` a concurrent insert referencing this user row takes) —
    # and hold it across every Stripe call below: the open-subscription check,
    # expiring the coach's other open Checkouts, and creating the new one must
    # happen as one atomic step for a single coach, or a second request can
    # still slip through the gap between them exactly like the bug this fixes.
    # The customer itself is deliberately created ABOVE, outside this block
    # (see the comment there) — only the Checkout-creation steps need the
    # lock's atomicity.
    with transaction.atomic():
        User.objects.select_for_update(no_key=True).filter(pk=request.user.pk).first()
        # The loser of the lock wakes up holding stale reads — the winner may
        # have just changed exactly what these checks depend on (the
        # customer id above all).
        request.user.refresh_from_db(fields=["stripe_customer_id"])
        sub = CoachSubscription.objects.filter(coach=request.user).first()
        # A comped coach has no Subscribe button — any Subscribe POST from one is
        # stale (an admin comped them between page load and this POST, with or
        # without a stray `first_charge` marker) and must not open a real
        # Checkout (#556, item 6).
        if sub and sub.status == CoachSubscription.Status.COMPED:
            messages.info(request, "Your plan changed. Nothing was charged.")
            return redirect("meso:billing")
        # Don't open a second Checkout for a coach who already has a live Stripe
        # subscription — completing it would create a duplicate (double-billing).
        # They manage the existing one in the Portal; a canceled mirror re-subscribes
        # freely. A Stripe trial (#555) counts as live here too: it already has a
        # real subscription, just not yet a charge.
        if sub and sub.has_live_stripe_subscription:
            messages.info(
                request,
                "You already have a subscription. Manage it in Manage billing.",
            )
            return redirect("meso:billing")
        # The local mirror can lag the webhook by a few seconds — long enough for
        # a double-click, or an older Checkout tab, to slip a second subscription
        # past the mirror-only guard above (#556, item 2). Ask Stripe directly.
        try:
            open_sub = billing_gateway.customer_has_open_subscription(request.user)
        except Exception:  # noqa: BLE001 — fail closed, never silently charge
            logger.exception(
                "Stripe subscription check failed for coach %s", request.user.pk
            )
            messages.error(request, STRIPE_UNAVAILABLE_MESSAGE)
            return redirect("meso:billing")
        if open_sub:
            # The mirror hasn't seen this subscription yet (its webhook is most
            # likely still in flight) — land on the billing page's pending
            # state instead of a bare bounce. Stripe just told us a
            # subscription exists, so set the pending marker directly rather
            # than a ``?billing=success`` round trip a plain "Billing" nav
            # click would immediately drop.
            messages.info(
                request,
                "You already have a subscription. Manage it in Manage billing.",
            )
            request.session[CHECKOUT_PENDING_SESSION_KEY] = timezone.now().timestamp()
            request.session.pop(CHECKOUT_STARTED_SESSION_KEY, None)
            return redirect("meso:billing")
        # A coach subscribing during their local trial keeps the rest of it (#555):
        # ``deferred_first_charge`` is the local trial_end when there's enough of it
        # left for Stripe to accept as ``subscription_data.trial_end``, else None.
        trial_end = billing_access.deferred_first_charge(request.user, sub=sub)
        # Stale-page guard: the billing page promised a deferred charge, rendering
        # a hidden ``first_charge=<unix timestamp>`` on the Subscribe form carrying
        # the *promised* date (#555 P2) — but the trial may have since dropped
        # under the 48h+margin threshold, or its end date moved (an admin edit, a
        # Stripe event) between page load and this POST. Either way, don't
        # silently charge on a date other than what the coach saw.
        promised = request.POST.get("first_charge")
        if promised:
            if trial_end is None:
                # `deferred_first_charge` is also None once the row is no longer
                # a local trial at all (round 2 nit) — e.g. the coach subscribed
                # and canceled in another tab between page load and this POST.
                # "your trial has less than 2 days left" would be wrong there, and
                # for a trial that has already lapsed; keep it only for a live
                # local trial with a clock.
                still_local_trial = (
                    sub is not None
                    and sub.status == CoachSubscription.Status.TRIALING
                    and not sub.stripe_subscription_id
                    and sub.trial_end is not None
                    and sub.is_active
                )
                if still_local_trial:
                    messages.info(
                        request,
                        "Your trial has less than 2 days left, so subscribing now "
                        "starts billing today. Click Subscribe again to continue.",
                    )
                else:
                    messages.info(
                        request,
                        "Your plan changed since this page loaded, so subscribing "
                        "now starts billing today. Click Subscribe again to "
                        "continue.",
                    )
                return redirect("meso:roster")
            if promised != str(int(trial_end.timestamp())):
                messages.info(
                    request,
                    "Your trial end date changed since this page loaded. Check "
                    "when you'll be charged and click Subscribe again.",
                )
                return redirect("meso:roster")
        # Right before opening the new Checkout, expire the customer's other open
        # subscription Checkout Sessions (#556, item 2) — the open-subscription
        # check above can't stop an *older* tab that was already open before the
        # first subscription existed, so this is what keeps only the newest one
        # completable.
        try:
            billing_gateway.expire_open_subscription_checkouts(request.user)
        except billing_gateway.SubscriptionCheckoutCompleted:
            # The coach finished paying in another tab while this request was
            # mid-flight (#556 review, round 3) — a subscription exists now,
            # even though the check above didn't see one yet. Same landing as
            # the Stripe-reported-subscription branch: the mirror is about to
            # catch up, so show the "finishing" state rather than a Subscribe
            # button that would open a second billable Checkout.
            logger.info(
                "Checkout completed under coach %s while opening another; "
                "bouncing instead of opening a second one.",
                request.user.pk,
            )
            messages.info(
                request,
                "You already have a subscription. Manage it in Manage billing.",
            )
            request.session[CHECKOUT_PENDING_SESSION_KEY] = timezone.now().timestamp()
            request.session.pop(CHECKOUT_STARTED_SESSION_KEY, None)
            return redirect("meso:billing")
        except Exception:  # noqa: BLE001 — fail closed, never silently charge
            logger.exception(
                "Stripe checkout expiry failed for coach %s", request.user.pk
            )
            messages.error(request, STRIPE_UNAVAILABLE_MESSAGE)
            return redirect("meso:billing")
        roster_url = request.build_absolute_uri(reverse("meso:roster"))
        try:
            session = billing_gateway.create_subscription_checkout_session(
                request.user,
                success_url=f"{roster_url}?billing=success",
                cancel_url=f"{roster_url}?billing=cancel",
                trial_end=trial_end,
            )
        except Exception:  # noqa: BLE001 — surface a friendly error, never a 500
            logger.exception(
                "Stripe checkout session failed for coach %s", request.user.pk
            )
            messages.error(request, "Could not start checkout. Please try again.")
            return redirect("meso:roster")
        # Carries the Checkout Session's own id, not just a timestamp (round 2
        # of the adversarial review, Fix B) — ``_checkout_pending`` verifies
        # THIS session actually completed before trusting a later
        # ``?billing=success``, rather than trusting the query param alone.
        request.session[CHECKOUT_STARTED_SESSION_KEY] = {
            "id": session.id,
            "at": timezone.now().timestamp(),
        }
        return redirect(session.url)


@login_required
@require_POST
def billing_portal(request):
    """Open Stripe's hosted Customer Portal so the coach can manage billing."""
    if not _is_coach(request.user):
        return redirect("meso:roster")
    # Sandbox gate (S4): no real Stripe customer behind a throwaway account.
    if meso_sandbox.is_sandbox(request.user):
        messages.info(request, "Billing is disabled in the demo.")
        return redirect("meso:roster")
    if not request.user.stripe_customer_id:
        messages.error(request, "You don't have a subscription to manage yet.")
        return redirect("meso:roster")
    return_url = request.build_absolute_uri(reverse("meso:roster"))
    try:
        session = billing_gateway.create_billing_portal_session(
            request.user, return_url=return_url
        )
    except Exception:  # noqa: BLE001 — surface a friendly error, never a 500
        logger.exception("Stripe portal session failed for coach %s", request.user.pk)
        messages.error(request, "Could not open the billing portal. Please try again.")
        return redirect("meso:roster")
    return redirect(session.url)


@login_required
@require_POST
def billing_start_trial(request):
    """Start the no-card 14-day local trial for a coach (S6 Phase 3, D3).

    The free path to the full toolkit — no Stripe, no card. Get-or-creates the
    coach's ``CoachSubscription`` row and flips it ``trialing`` for
    ``TRIAL_DAYS``. Single-use: a coach who has already trialed (even if it
    lapsed) gets a friendly "already used" notice, never a 500. Coach-surface
    only; a non-coach is bounced to the roster (which redirects them home).

    Sandbox gate (S4): a sandbox coach never starts a real trial.
    """
    if not _is_coach(request.user):
        return redirect("meso:roster")
    if meso_sandbox.is_sandbox(request.user):
        messages.info(request, "Billing is disabled in the demo.")
        return redirect("meso:roster")
    try:
        CoachSubscription.start_trial_for(request.user)
    except InvalidTransition:
        messages.info(request, "You've already used your free trial.")
    else:
        messages.success(
            request,
            f"Your {CoachSubscription.TRIAL_DAYS}-day free trial has started — "
            "the full Meso toolkit is unlocked.",
        )
    return redirect("meso:roster")


# -- self-serve coach signup (S6 Phase 4, D11) ----------------------------
#
# The public funnel that turns a visitor into a coach. Until now a
# ``CoachProfile`` was created only by admin or the demo seed (B1 made Meso a
# multi-coach SaaS, but there was no front door). The landing page pitches the
# plan tiers; ``start_coaching`` creates the ``CoachProfile``. Plan choice after
# signup is the existing Phase 3 roster billing card — this slice only needs to
# create the coach. See ``docs/meso/billing-plan.md``.


class BecomeCoachView(TemplateView):
    """Public "become a coach" landing — the front door to self-serve signup.

    Pitches Meso coaching and the plan tiers (free / no-card trial / per-seat
    paid), then routes the visitor:

    - an **existing coach** has no use for the pitch → straight to the roster;
    - an **anonymous** visitor is sent through allauth signup/login first (the
      template offers those CTAs with ``?next=`` back here) — the POST action is
      login-required and a login redirect would return as a GET it rejects;
    - a **logged-in non-coach** sees the "start coaching" form.
    """

    template_name = "meso/become_coach.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated and _is_coach(request.user):
            return redirect("meso:roster")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["free_seats"] = CoachSubscription.FREE_SEAT_LIMIT
        ctx["trial_days"] = CoachSubscription.TRIAL_DAYS
        ctx["free_agent_runs"] = CoachSubscription.FREE_AGENT_ALLOWANCE
        ctx["paid_agent_runs"] = CoachSubscription.PAID_AGENT_ALLOWANCE
        ctx["price_summary"] = presenters.PRICE_SUMMARY
        # allauth returns here after signup/login (?next=), where the visitor —
        # now authenticated — sees the start-coaching form.
        ctx["next_url"] = reverse("meso:become_coach")
        return ctx


@login_required
@require_POST
def start_coaching(request):
    """Create the coach's ``CoachProfile`` and land them on the roster (Phase 4).

    The funnel's payoff: turns a logged-in visitor into a coach. Idempotent — a
    user who already has a profile just goes to the roster (a re-POST / double
    submit is harmless). With ``plan=trial`` it also starts the no-card local
    trial in the same step (single-use; an already-trialed coach is silently left
    as-is, never a 500). The free path creates **no** subscription row — free is
    "no row" — and subscribing is the roster's Subscribe CTA (Phase 3).
    """
    CoachProfile.objects.get_or_create(user=request.user)
    started_trial = False
    if request.POST.get("plan") == "trial":
        try:
            CoachSubscription.start_trial_for(request.user)
        except InvalidTransition:
            # Already trialed (e.g. a returning coach) — keep their current state.
            pass
        else:
            started_trial = True
    if started_trial:
        messages.success(
            request,
            f"Welcome! Your {CoachSubscription.TRIAL_DAYS}-day free trial has "
            "started — the full Meso toolkit is unlocked.",
        )
    else:
        messages.success(
            request,
            "Welcome to Meso coaching! Invite your first athlete to get started.",
        )
    return redirect("meso:roster")


@csrf_exempt
@require_POST
def billing_webhook(request):
    """Stripe billing webhook — verify, then mirror subscription state locally.

    A separate endpoint (and signing secret) from the products webhook (D9). An
    unsigned/unverifiable request is a 400; a verified event is applied
    idempotently and answered 200.
    """
    sig_header = request.headers.get("stripe-signature")
    if sig_header is None:
        return HttpResponse(status=400)
    try:
        event = billing_webhooks.construct_event(request.body, sig_header)
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)
    billing_webhooks.handle_event(event)
    return HttpResponse(status=200)


# -- still on fixtures until their own slices ------------------------------


class ChangeReviewView(LoginRequiredMixin, TemplateView):
    """Review the batch of edits the agent proposes before they hit the program.

    ``review/<batch_id>/`` renders a real, owned ``AgentProposalBatch``; the coach
    approves/rejects per change and applies the batch (Phase 2). The bare
    ``review/`` redirects to the coach's latest pending batch (fixtures retired).
    """

    template_name = "meso/review.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("batch_id") is None:
            # The latest pending batch across *any* plan the coach owns — a
            # proposal on one athlete shouldn't be missed because another is the
            # working plan.
            batch = (
                AgentProposalBatch.objects.filter(
                    plan__in=Plan.objects.editable_by(request.user),
                    status=AgentProposalBatch.Status.PENDING,
                )
                .order_by("-created_at")
                .first()
            )
            if batch is None:
                messages.info(request, "No proposals to review yet.")
                return redirect("meso:designer")
            return redirect("meso:review_batch", batch_id=batch.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        batch = (
            AgentProposalBatch.objects.filter(
                pk=kwargs["batch_id"],
                plan__in=Plan.objects.editable_by(self.request.user),
            )
            .select_related("plan", "plan__relationship__athlete")
            .first()
        )
        if batch is None:
            raise Http404("Unknown proposal batch")
        ctx.update(presenters.review_changes(batch))
        ctx["batch_id"] = batch.pk
        ctx["plan_id"] = batch.plan_id
        ctx["is_pending"] = batch.status == AgentProposalBatch.Status.PENDING
        return ctx


class DeliverView(LoginRequiredMixin, TemplateView):
    """Confirm what gets sent to the athlete, when, and how.

    The screen binds to a real, owned plan: it shows that plan's athlete +
    current week and its "Deliver" button POSTs to ``plan_deliver`` (stamp +
    snapshot). The bare URL redirects to the coach's working plan (or the
    roster) now that the prototype fixtures are retired (Phase 5).
    """

    template_name = "meso/deliver.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("plan_id") is None:
            plan = _coach_working_plan(request.user)
            if plan is None:
                messages.info(request, "Pick an athlete to deliver a program.")
                return redirect("meso:roster")
            return redirect("meso:deliver_plan", plan_id=plan.pk)
        # A template plan has no athlete to deliver to (parity plan §3.4) —
        # bounce back to the designer instead of a confusing 404. (Fanning
        # copies out to clients FROM a template works at the endpoint level —
        # ``plan_batch_deliver`` — but has no screen yet.)
        template = Plan.objects.filter(
            pk=kwargs["plan_id"], is_template=True, owner=request.user
        ).first()
        if template is not None:
            messages.info(
                request,
                "Templates aren't delivered — deliver a client's copy instead.",
            )
            return redirect("meso:designer_plan", plan_id=template.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        plan = (
            Plan.objects.for_coach(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        ctx["plan_id"] = plan.pk
        ctx.update(presenters.deliver_screen(plan, week=self._target_week(plan)))
        # Batch-deliver (2c, parity plan §3.1): the coach's OTHER deliverable
        # clients, offered as "send each an independent copy" checkboxes.
        # Soft-suspended (over-seat-limit, D6) links are omitted — the POST
        # re-checks, this just keeps the screen honest.
        ctx["batch_candidates"] = [
            {"id": rel.pk, "name": rel.athlete.display_name()}
            for rel in CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .exclude(pk=plan.relationship_id)
            .exclude(pk__in=billing_access.suspended_athlete_ids(self.request.user))
            .select_related("athlete")
            .order_by("athlete__name", "athlete__email")
        ]
        return ctx

    def _target_week(self, plan):
        """The week the deliver screen targets, from the ``?week=`` query param.

        Resolves ``?week=`` to a *live* week of this plan, or None (the presenter
        falls back to the live week). A missing / foreign / removed / non-numeric
        ``week`` is ignored rather than a 404: the confirm screen always renders
        something deliverable, and the deliver POST itself validates the chosen
        week strictly (it 404s a soft-deleted target — the screen must agree).
        """
        raw = self.request.GET.get("week")
        if not raw:
            return None
        try:
            week_id = int(raw)
        except (TypeError, ValueError):
            return None
        return Week.objects.filter(
            pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
        ).first()


class ResultsView(LoginRequiredMixin, TemplateView):
    """Logged session results vs targets — closes the loop back to the agent.

    Binds to a real, owned session (``results/<session_id>/``): the athlete's
    logged sets scored against the prescribed grid (athlete slice Phase 3, the
    coach-side fixtures retired). The bare ``results/`` redirects to the coach's
    most-recently-logged session, or back to the roster if none — mirroring the
    designer/deliver bare redirects.
    """

    template_name = "meso/results.html"

    def get(self, request, *args, **kwargs):
        if kwargs.get("session_id") is None:
            session = _coach_latest_logged_session(request.user)
            if session is None:
                messages.info(request, "No logged sessions yet.")
                return redirect("meso:roster")
            return redirect("meso:results_session", session_id=session.pk)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = _coach_session_or_404(self.request.user, kwargs["session_id"])
        ctx["active"] = "roster"
        ctx.update(presenters.session_results(session))
        return ctx
