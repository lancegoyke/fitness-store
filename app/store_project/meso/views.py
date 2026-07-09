import datetime
import ipaddress
import json
import logging
import uuid
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
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
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

from store_project.notifications.emails import send_block_delivered_email
from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.emails import send_week_delivered_email

from . import adherence as meso_adherence
from . import demo as meso_demo
from . import one_rm as meso_one_rm
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
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachProfile
from .models import CoachSubscription
from .models import ExerciseSlot
from .models import GroupMembership
from .models import InvalidTransition
from .models import LoadType
from .models import LoggedSet
from .models import Mesocycle
from .models import MesoGroup
from .models import Plan
from .models import PlanAction
from .models import Prescription
from .models import PrescriptionOverride
from .models import ProposedChange
from .models import PushSubscription
from .models import SandboxSession
from .models import Session
from .models import SessionLog
from .models import SessionSlot
from .models import Week
from .models import WeekDelivery
from .serializers import current_week
from .serializers import group_adjustments
from .serializers import serialize_chat_thread
from .serializers import serialize_mesocycle_grid
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
    ``plans`` scopes the candidate set — the *designer* passes
    ``Plan.objects.editable_by(user)`` so a group shared program can be the
    working plan too (the designer handles both kinds), while deliver keeps the
    individual-only default (``for_coach``) since deliver-to-all is Phase 4.
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
            # The designer opens individual *or* group plans, so its bare-URL
            # target spans both (``editable_by``) — a coach who just edited a
            # group's shared program lands back on it, not an older individual one.
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
        # ``editable_by`` (not ``for_coach``) so a *group* shared program — rooted
        # at a group the coach owns — opens here too, not just individual plans.
        plan = (
            Plan.objects.editable_by(self.request.user)
            .filter(pk=kwargs["plan_id"])
            .first()
        )
        if plan is None:
            raise Http404("Unknown plan")
        ctx["plan_data"] = serialize_plan(plan)
        # P1 multi-week table (backend): the current block's dense day × row ×
        # week grid, hydrated alongside ``plan_data`` so the table can render
        # without a round-trip on first paint. Uses the same block resolution
        # as ``serialize_plan`` (the current week's mesocycle); left unset for
        # a plan with no block at all (shouldn't happen post-scaffold, but a
        # corrupt/legacy row shouldn't 500 the whole designer).
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
        return ctx


class RosterView(TemplateView):
    """The front door (``/meso/``) — splits on auth (first-time-UX Phase 3).

    - An **anonymous** visitor sees the public landing (what Meso is + two honest
      entry actions: log in as an athlete, or become a coach) rather than a bare
      login wall — Meso has to be legible before you have an account.
    - An **authenticated** visitor keeps the post-#311 role routing. The roster
      is a *coach* surface, so anyone not acting as a coach is sent to their
      training home — where they see delivered programs, respond to a coach's
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
                    # #388) — settings-driven so `just record-demo && just
                    # publish-demo-video` is the entire refresh story; an empty
                    # override hides the section (template checks
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
        # ``working_plan``: non-archived, not a materialized group snapshot). The
        # roster hides the "Draft with AI" CTA for these — ``plan_create`` reopens
        # an existing plan rather than drafting, so the action would be a no-op.
        have_plan = set(
            Plan.objects.filter(
                relationship_id__in=[link.pk for link in links],
                source_group__isnull=True,
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
                # Adherence to the athlete's latest delivered week (read-side
                # aggregation over their done logs); ``None`` hides the meter.
                compliance=meso_adherence.link_compliance(link),
            )
            for link in links
        ]
        groups = (
            MesoGroup.objects.for_coach(self.request.user)
            .active()
            .prefetch_related("memberships__relationship__athlete")
        )
        ctx["active"] = "roster"
        ctx["athletes"] = athletes
        # Groups (S1 Phase 1) read real rows; the shared program + per-athlete
        # auto-adjusts land in groups Phase 2/3.
        ctx["groups"] = [presenters.roster_group(g) for g in groups]
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
        ctx["billing"] = presenters.billing_state(self.request.user)
        # Recent-activity feed: the coach's athletes' latest completed sessions.
        ctx["activity"] = presenters.roster_activity(self.request.user)
        # Needs-review (agent batch state) is a separate slice — still neutral.
        ctx["needs_review"] = 0
        # First-run UX (Phase 2): a fresh coach with nothing yet gets an
        # onboarding card that teaches the model and offers the one-click demo;
        # once demo data is loaded a banner offers to remove it (Q3).
        ctx["has_demo"] = meso_demo.has_demo(self.request.user)
        ctx["is_empty"] = not athletes and not ctx["groups"]
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
        # Light up the program block off the athlete's delivered reality (current
        # block/week, the macrocycle rail, adherence, status, latest session). The
        # athlete identity record carries the program overlay merged in.
        athlete = presenters.profile_athlete(link.athlete)
        program = presenters.profile_program(link, working_plan)
        athlete.update(program["athlete"])
        ctx["athlete"] = athlete
        ctx["macrocycle"] = program["macrocycle"]
        ctx["results_summary"] = program["results_summary"]
        ctx["coach_style"] = presenters.coach_style(self.request.user)
        # Whether to offer "Draft with AI" on the create CTA — the same agent
        # allowance gate the endpoint enforces (the draft *is* an agent run).
        ctx["can_use_agent"] = billing_access.can_use_agent(self.request.user)
        return ctx


class GroupDetailView(LoginRequiredMixin, TemplateView):
    """A coach's training group — members + their cross-group flags (S1 Phase 1).

    Coach-scoped: a foreign or unknown group is a flat 404. The shared program +
    per-athlete auto-adjusts land in groups Phase 2/3; this is the read surface.
    """

    template_name = "meso/group_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        group = (
            MesoGroup.objects.for_coach(self.request.user)
            .filter(pk=kwargs["pk"])
            .first()
        )
        if group is None:
            raise Http404("Unknown group")
        ctx["active"] = "roster"
        ctx["group"] = presenters.group_detail(group)
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


class CoachBillingView(LoginRequiredMixin, TemplateView):
    """Coach-facing billing & plan page (agent-usage — coach surface).

    A coach's own plan/tier, the bill they owe (base + per active seat), the
    upgrade CTAs, and their AI-agent runs this month broken down per athlete/group
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
        ctx.update(presenters.coach_billing(self.request.user))
        return ctx


def _coach_active_athletes(coach, athlete_ids):
    """Resolve posted athlete ids to this coach's *active*-link athletes.

    Sanitizes each id to a UUID (a malformed value is skipped, never reaching the
    ORM as a query error) and scopes to the coach's own active links, so only
    their current athletes resolve — a foreign or stale pick simply drops out.
    The order follows the posted ids' resolution, deduped by the link set.
    """
    valid_ids = []
    for raw in athlete_ids:
        try:
            valid_ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue
    if not valid_ids:
        return []
    links = (
        CoachAthlete.objects.for_coach(coach)
        .active()
        .filter(athlete_id__in=valid_ids)
        .select_related("athlete")
    )
    return [link.athlete for link in links]


@login_required
@require_POST
def group_create(request):
    """Create a new group from the roster: name + focus + picked members (Phase 2b).

    A normal form POST (not JSON), the roster's "New group" disclosure. ``name``
    is required; ``focus`` is optional; ``athletes`` is the multi-valued list of
    picked athlete ids, each resolved against the coach's *active* links so only
    their own current athletes can be added (a foreign/stale/malformed pick is
    silently ignored — ``MesoGroup.create_for_coach`` carries the same tenancy
    guard). Lands on the new group's detail page, where the coach designs its
    shared program. A blank name creates nothing and returns to the roster (the
    field is also ``required`` client-side).
    """
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, "A group needs a name.")
        return redirect("meso:roster")
    focus = (request.POST.get("focus") or "").strip()
    athletes = _coach_active_athletes(request.user, request.POST.getlist("athletes"))
    group = MesoGroup.create_for_coach(
        request.user, name=name, focus=focus, athletes=athletes
    )
    # #441 P3-5: the groups step auto-advances once the group exists. A no-op
    # unless the coach is parked on groups.
    meso_tour.advance_if_on_step(request.user, "groups")
    return redirect("meso:group", pk=group.pk)


@login_required
@require_POST
def group_design(request, pk):
    """Open (or create) a group's shared program and land in the designer (Phase 2).

    Coach-scoped: a foreign or unknown group is a flat 404. Idempotent — reuses
    the group's existing non-archived shared plan, only creating (with a starter
    scaffold) when there is none. The lookup + create run under a row lock on the
    group so two concurrent submits can't both see "no plan" and each create one
    (the second waits, then reuses the first's plan).
    """
    with transaction.atomic():
        group = (
            MesoGroup.objects.select_for_update()
            .for_coach(request.user)
            .filter(pk=pk)
            .first()
        )
        if group is None:
            raise Http404("Unknown group")
        # Edit gate (D6): an over-limit coach is frozen out of designing programs.
        if not billing_access.can_edit(request.user):
            messages.error(request, OVER_LIMIT_MESSAGE)
            return redirect("meso:group", pk=group.pk)
        plan = group.shared_plan() or group.create_shared_plan()
    return redirect("meso:designer_plan", plan_id=plan.pk)


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
    User.objects.select_for_update().filter(pk=request.user.pk).first()
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
    return agent_service.create_drafting_batch(
        plan,
        agent_service.DRAFT_INSTRUCTION,
        coach=request.user,
        trigger=AgentProposalBatch.Trigger.DRAFT,
    )


@login_required
@require_POST
def plan_create(request, pk):
    """Create (or open) an individual program for one of the coach's athletes.

    The individual analogue of ``group_design`` — the action behind the
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
    # Dispatch (and bump the plan) outside the lock, mirroring ``agent_propose``.
    if draft_batch is not None:
        agent_jobs.dispatch_proposal(draft_batch.pk)
        _touch_plan(plan)
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
    # #441 P3-5: the designer/agent steps auto-advance once the plan exists —
    # ``natural_step`` already names which of the two fired (agent when drafting,
    # else designer). A no-op unless the coach is parked on that step.
    meso_tour.advance_if_on_step(request.user, natural_step)
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
    "group": "Sample group added — shared programming + per-athlete overrides.",
}


@login_required
@require_POST
def demo_load(request):
    """Load a coach-scoped demo workspace so a new coach can explore (Q3, Phase 2).

    A populated, **clearly-labeled, fully-removable** workspace — five athletes, a
    built/delivered/logged program, and a group — scoped to this coach, idempotent,
    billing-neutral, and silent (no demo-athlete email/push). Lands on the roster
    where the data now shows, with a "Remove demo data" affordance.

    An optional ``segment`` POST field narrows the load to one slice of the demo
    (``meso_demo.SEGMENTS`` — ``athletes``/``program``/``delivery``/``log``/``group``)
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
    # Like demo_load: adding yourself is an implicit "I'm coaching now", so make
    # sure the CoachProfile exists rather than minting a coach via a side door.
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
    # roster — no manual Next needed. A no-op unless parked on welcome.
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


@login_required
@require_POST
def group_deliver(request, pk):
    """Deliver a group's shared current week to every active member (Phase 4).

    The coach-facing entry — a plain form POST from the group-detail page's
    "Deliver this week to all members" button. Coach-scoped (a foreign or unknown
    group is a flat 404). Requires a shared program (else a flashed prompt to
    design one) and at least one member (else a flashed error from the fan-out);
    on success it flashes how many members were delivered to. Always lands back on
    the group-detail page.
    """
    group = MesoGroup.objects.for_coach(request.user).filter(pk=pk).first()
    if group is None:
        raise Http404("Unknown group")
    # Deliver gate (D6): an over-limit coach can't deliver until back within the cap.
    if not billing_access.can_edit(request.user):
        messages.error(request, OVER_LIMIT_MESSAGE)
        return redirect("meso:group", pk=group.pk)
    plan = group.shared_plan()
    if plan is None:
        messages.error(request, "Design a shared program before delivering.")
        return redirect("meso:group", pk=group.pk)
    summary, error = _fan_out_group_delivery(request, plan)
    if error is not None:
        messages.error(request, error)
    else:
        n = summary["members"]
        messages.success(
            request,
            f"Delivered this week to {n} member{'' if n == 1 else 's'}.",
        )
    return redirect("meso:group", pk=group.pk)


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's own logged-in surface, distinct from the coach's view of an
# athlete (``/meso/athlete/<uuid>/``). Read-only here; logging lands in Phase 2.
# Everything is scoped to the athlete's *active* coaches (``for_athlete``), to
# **delivered** weeks (delivery gates *visibility* — an undelivered week is
# hidden; a delivered week's *current* contents are shown, see
# ``latest_delivered_week``), and to non-archived plans. An out-of-scope session
# is a flat 404 — never a silent empty render.


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
    """A delivered session the athlete owns, or ``Http404``.

    404 unless the session's week is delivered *and* its plan is one the athlete
    reaches through an active coach link — a foreign athlete, an undelivered
    week, an archived plan, or an unknown id are indistinguishable (no leak).

    Soft delete (designer framework Phase 0): a session the coach removed —
    or one under a removed week — is gone from the athlete's surface too, even
    if it was already delivered. Cells are read live via ``session.cells()``
    (P0 fixed-lineup cutover), so every downstream call (the logger grid,
    ``_clean_logged_sets``'s allowed ids, ``athlete_set_one_rm``) sees only
    live rows. Already-logged history is untouched — those reads go through
    ``SessionLog``/``LoggedSet``, never this lookup.
    """
    session = (
        Session.objects.filter(
            pk=pk,
            week__delivered_at__isnull=False,
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


class AthleteHomeView(LoginRequiredMixin, TemplateView):
    """The athlete's training home: their delivered programs, this week."""

    template_name = "meso/athlete_home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "training"
        ctx["plans"] = presenters.athlete_home(self.request.user)
        # Pending coach links (N4 Phase 2): invites awaiting my reply + requests
        # I've sent + the request-a-coach form all live on this surface.
        ctx["pending"] = presenters.athlete_pending(self.request.user)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        # First-log coachmark (Phase 4): only when there's a delivered session to
        # tap *and* the athlete has never logged — pointing "tap a session below"
        # at an empty week would be noise.
        has_delivered = any(card["sessions"] for card in ctx["plans"])
        ctx["show_first_log_hint"] = has_delivered and not _athlete_has_completed_log(
            self.request.user
        )
        ctx.update(_pwa_context())
        return ctx


class AthleteSessionView(LoginRequiredMixin, TemplateView):
    """One delivered session — the athlete's interactive logger (Phase 2).

    Renders the prescribed grid as set-input rows pre-filled from the athlete's
    own existing log, and injects ``log_data`` for the Alpine logger to hydrate
    from and POST back to ``athlete_log_session``.
    """

    template_name = "meso/athlete_session.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        session = _athlete_session_or_404(self.request.user, kwargs["pk"])
        sess = presenters.athlete_session(session, self.request.user)
        ctx["active"] = "training"
        ctx["session"] = sess
        ctx["log_data"] = presenters.athlete_log_payload(sess)
        ctx["athlete_name"] = self.request.user.display_name()
        ctx["athlete_initials"] = presenters.initials(ctx["athlete_name"])
        # First-log coachmark (Phase 4): teach the logger only to a first-ever
        # logger — any prior log means they already know how.
        ctx["show_first_log_hint"] = not _athlete_has_completed_log(self.request.user)
        ctx.update(_pwa_context())
        return ctx


# Free-form text cells per logged set, mapped to their model ``max_length``.
LOG_SET_FIELDS = {"reps": 32, "load": 32, "rpe": 32}
# A generous ceiling on a set's number — no real session has this many sets, and
# bounding it here stops a malformed client from storing an enormous ``set_number``
# that would later balloon the session page's set-row render (presenters._set_rows).
MAX_LOGGED_SET_NUMBER = 50


@login_required
@require_POST
def athlete_log_session(request, pk):
    """Upsert the athlete's log for a delivered session they own (Phase 2).

    Replaces the athlete's own ``SessionLog`` + ``LoggedSet`` rows for this
    session with the posted state, flips the session done (unless an explicit
    ``status`` says otherwise), and stamps the date (today when none is given).
    Scoped by ``_athlete_session_or_404`` — a foreign, undelivered, archived, or
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

    with transaction.atomic():
        log = (
            SessionLog.objects.filter(session=session, athlete=request.user)
            .order_by("-created_at")
            .first()
        )
        if log is None:
            log = SessionLog(session=session, athlete=request.user)
        log.status = status
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
        log.sets.filter(
            prescription_id__in=[p.pk for p in session.trainable_cells()]
        ).delete()
        LoggedSet.objects.bulk_create(
            [
                LoggedSet(
                    session_log=log,
                    prescription_id=cs["prescription_id"],
                    set_number=cs["set_number"],
                    reps=cs["reps"],
                    load=cs["load"],
                    rpe=cs["rpe"],
                )
                for cs in cleaned_sets
            ]
        )
        # Refresh the athlete's persisted 1RM for this session's lifts from their
        # *completed* logs. Run on every save, not only a done one: derivation
        # counts done logs only, so refreshing after a done→pending downgrade (this
        # session is no longer a finished performance) clears an estimate that's
        # now unsupported. Recomputes from scratch — a heavier set raises it, an
        # edit that drops the PR lowers it, a removed basis clears it.
        meso_one_rm.refresh_one_rms(
            request.user,
            list(session.trainable_cells()),
            session.week.mesocycle.plan.unit,
        )
    # #441 P3-5: the results step auto-advances once the coach *completes* one of
    # their own sessions (the self-coaching coach logs here). Only a ``done`` log
    # counts — a ``pending`` "save progress" isn't a result yet, so it must not
    # skip the step (matches ``_self_has_log``). A no-op unless parked on results.
    if log.status == SessionLog.Status.DONE:
        meso_tour.advance_if_on_step(request.user, "results")
    return JsonResponse({"ok": True, "log": serialize_session_log(log)})


@login_required
@require_POST
def athlete_set_one_rm(request, pk):
    """Set or clear the athlete's *manual* 1RM for a lift in a session they own.

    The estimated 1RM was per-device localStorage (Phase 2b); this persists the
    athlete's typed value server-side as a ``source=manual`` ``AthleteOneRm`` so
    it syncs across devices and is visible to the coach. The body is
    ``{"prescription": <id>, "value": "140"}`` — a blank/absent ``value`` *clears*
    it back to the log-derived estimate. Scoped exactly like the log endpoint
    (``_athlete_session_or_404``): the prescription must live in a delivered
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


def _clean_logged_sets(raw_sets, session):
    """Validate the posted ``sets`` against this session, or return a 400.

    Returns ``(cleaned, None)`` on success or ``(None, HttpResponseBadRequest)``.
    Every set must reference a prescription **in this session** (no foreign rows),
    carry a positive integer ``set_number`` (defaulting to its position), and have
    string reps/load/rpe within the model's ``max_length``.
    """
    if not isinstance(raw_sets, list):
        return None, HttpResponseBadRequest("sets must be a list.")
    # Only trainable rows are postable — the logger never renders a skipped cell.
    allowed_ids = {p.pk for p in session.trainable_cells()}
    cleaned = []
    seen = set()
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
        fields = {}
        for field, max_length in LOG_SET_FIELDS.items():
            value = raw.get(field, "")
            if not isinstance(value, str):
                return None, HttpResponseBadRequest(f"{field} must be a string.")
            if len(value) > max_length:
                return None, HttpResponseBadRequest(f"{field} is too long.")
            fields[field] = value
        cleaned.append(
            {"prescription_id": presc_id, "set_number": set_number, **fields}
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
PWA_CACHE_VERSION = "meso-pwa-v3"


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

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"athlete": request.user, "p256dh": p256dh, "auth": auth},
    )
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

    existing = CoachAthlete.objects.filter(coach=coach, athlete=request.user).first()
    if existing and existing.is_active:
        messages.info(request, f"You're already training with {coach.display_name()}.")
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

    CoachAthlete.request(athlete=request.user, coach=coach)
    athlete = request.user
    roster_url = request.build_absolute_uri(reverse("meso:roster"))

    def _send():
        try:
            send_coach_request_email(
                athlete=athlete, coach=coach, roster_url=roster_url
            )
        except Exception:  # mail is best-effort; never fail the request on it
            logger.exception("Failed to send coach request email to %s", coach.email)

    transaction.on_commit(_send)
    messages.success(request, f"Request sent to {coach.display_name()}.")
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
    invite, _ = CoachInvite.open_for(coach=request.user, email=email)
    accept_url = request.build_absolute_uri(
        reverse("meso:invite_claim", kwargs={"token": invite.token})
    )
    coach = request.user

    def _send():
        try:
            send_coach_invite_email(coach=coach, email=email, accept_url=accept_url)
        except Exception:  # mail is best-effort; never fail the invite on it
            logger.exception("Failed to send coach invite email to %s", email)

    transaction.on_commit(_send)
    messages.success(request, f"Invite sent to {email}.")
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
            send_coach_invite_email(coach=coach, email=email, accept_url=accept_url)
        except Exception:  # mail is best-effort; never fail the resend on it
            logger.exception("Failed to resend coach invite email to %s", email)

    transaction.on_commit(_send)
    messages.success(request, f"Invite resent to {email}.")
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
        with transaction.atomic():
            # Lock by the *submitted token*, not the pk: a resend that rotated the
            # token out from under this in-flight claim must invalidate the old
            # link (Phase-3 "resend kills the previous token"), so a superseded
            # token finds no row → 404 rather than accepting on stale authority.
            invite = get_object_or_404(
                CoachInvite.objects.select_for_update(), token=token
            )
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

# Free-form text cells the grid edits, mapped to their model ``max_length``.
# ``name``/``exercise`` are NOT here (P0 fixed-lineup cutover): a cell's name
# resolves from its ``ExerciseSlot`` (block-wide identity) or a one-week
# ``swap_name`` — neither is writable through this per-cell numbers patch.
PATCHABLE_FIELDS = {
    "sets": 32,
    "reps": 32,
    "load": 32,
    "rpe": 32,
    "rest": 32,
    "note": 255,
}


def _coach_plan_or_forbidden(request, plan_id):
    """The plan the requester coaches, or an ``HttpResponseForbidden``.

    404 when the plan does not exist; 403 when it exists but the requester may
    not edit it — for an individual plan, its coach over an active relationship;
    for a group plan, the coach who owns the group (``Plan.is_editable_by``).
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

    # ``load_type`` is an enum, not free text: validate against the whitelist so a
    # bad value is a 400 (and nothing is persisted), not a stored garbage choice.
    if "load_type" in payload:
        load_type = payload["load_type"]
        if load_type not in LoadType.values:
            return HttpResponseBadRequest("Invalid load_type.")
        updates["load_type"] = load_type

    # ``name`` is identity. The row renders the cell's EFFECTIVE name (a one-week
    # swap, else the block-shared slot's), and the React client echoes it on every
    # blur — even for a sets/load edit — so treat it as an edit only when it
    # actually differs from the current effective name. A real rename of a SWAPPED
    # cell retargets that week's swap; of a normal cell, the block-shared
    # ``ExerciseSlot`` (the fixed-lineup rename, keeping the P0 designer's inline
    # name autosave working). This guard is what stops a swapped cell's routine
    # autosave from renaming the base row for the whole block.
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
                if cell.swap_name or cell.swap_exercise_id:
                    # Editing the shown name of a swapped week edits that week's
                    # swap only — never the block-shared slot.
                    cell.swap_name = name_edit
                    cell.swap_exercise = None
                    cell.save(update_fields=["swap_name", "swap_exercise"])
                else:
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
        cells_by_week[week.pk] = Prescription.objects.create(
            exercise_slot=exercise_slot,
            week=week,
            sets="3",
            reps="10",
            rpe="7",
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
        # Lock ordering: plan BEFORE any child row (undo/redo, the deletes, and
        # week_set_current all lock the plan first, then touch weeks) — taking
        # the mesocycle lock first here could deadlock against them.
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
            Prescription.objects.create(
                exercise_slot=exercise_slot, week=w, sets="3", reps="10", rpe="7"
            )
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
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
    """The block the P1 grid opens onto when no ``?mesocycle=`` is given.

    Mirrors ``serialize_plan``'s block resolution: the current (live) week's
    block, falling back to the plan's first block for the rare case where the
    plan has a block but no materialized weeks yet (a fresh, not-yet-designed
    block). ``None`` only when the plan has no block at all.
    """
    open_week = current_week(plan)
    if open_week is not None:
        return open_week.mesocycle
    return plan.mesocycles.order_by("order").first()


@login_required
@require_GET
def api_mesocycle_grid(request, plan_id):
    """The P1 multi-week table's data: every live day × row × week cell.

    A pure read (mirrors ``week_view``) — scoped by ownership only (404/403),
    **not** billing-gated: an over-limit coach keeps read access. Defaults to
    the plan's current mesocycle; ``?mesocycle=<id>`` views another block of
    the same plan (404 for one that doesn't belong to it, 400 for a
    non-integer). A plan with no block at all is a 404; a block with no
    materialized weeks yet returns a valid, empty-ish grid.
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

    The designer's "+ Add week": grows the mesocycle of the live (current) week —
    or, lacking one, the plan's last block — by copying its latest week's grid
    (``Mesocycle.append_week``). The new week is a non-current draft, so adding it
    never changes what's live or deliverable. Scoped + edit-gated like the other
    designer writes (403 foreign, 402 over-limit). Row-locks the mesocycle so two
    concurrent submits can't both read the same max index and collide on
    ``unique_week_index`` (explicit transaction — prod views run in autocommit).
    Returns the plan pinned to the new week so the client switches to it.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    open_week = current_week(plan)
    mesocycle = (
        open_week.mesocycle
        if open_week is not None
        else plan.mesocycles.order_by("order").last()
    )
    if mesocycle is None:
        return HttpResponseBadRequest("This plan has no block to add a week to.")
    with transaction.atomic():
        # Lock ordering: plan first (see session_add).
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
def week_set_current(request, plan_id, week_id):
    """Make ``week`` the plan's current week — its designer-default + deliver target.

    The designer's "Make current": flips the live pointer to the viewed week so
    delivery (which sends ``current_week``) targets it and the designer opens onto
    it next time. Post-P3 this pointer also means "the week the athlete is on":
    the athlete home (``presenters.athlete_home``) opens its block card onto the
    current week (when it's delivered) and takes "today's session" from it — the
    coach marks which week the athlete is on by setting it current here. (Setting
    it stays manual; auto-advance is out of scope.) Exactly one week is current —
    the others in the plan are cleared.
    Scoped + edit-gated (403 foreign, 402 over-limit); a foreign week is a 404.
    Row-locks the plan so concurrent set-currents serialize, and re-reads the
    week's liveness under that lock — a concurrent ``week_delete`` (which
    takes the same lock) could soft-delete this week while we wait, and a
    deleted week must never become current. Returns the plan pinned to the
    new current week.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(
        Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    )
    with transaction.atomic():
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
        week.refresh_from_db()
        if week.deleted_at is not None:
            raise Http404("Week not found.")
        already_current = week.is_current
        if not already_current:
            # Snapshot BEFORE the bulk clear below touches every other week's
            # ``is_current`` flag, so undo restores exactly who was current.
            record_plan_action(plan, f"Made Week {week.index} current")
        Week.objects.filter(mesocycle__plan=plan).exclude(pk=week.pk).update(
            is_current=False
        )
        if not already_current:
            week.is_current = True
            week.save(update_fields=["is_current"])
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


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

    Two rules gate the action itself (400, not the row's own 404 — it exists
    and is live): the **current** (deliver-target) week can't be deleted — the
    coach must make another week current first — and the plan's **last
    remaining live week** can't be deleted (a plan always needs at least one).
    Row-locks the plan (mirrors ``week_set_current``) and re-reads the row's
    flags under that lock, so a concurrent ``week_set_current`` or a second
    delete can't race the current-flag check or the last-live-week count.
    Response is *not* pinned to a week —
    ``serialize_plan`` falls back to the (untouched) current week, which the
    client uses to reopen even if the deleted week was the one being viewed.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(
        Week, pk=week_id, mesocycle__plan=plan, deleted_at__isnull=True
    )
    with transaction.atomic():
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
        week.refresh_from_db()
        if week.deleted_at is not None:
            raise Http404("Week not found.")
        if week.is_current:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Make another week current before removing this one.",
                },
                status=400,
            )
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
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
            Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
            Plan.objects.select_for_update().filter(pk=plan.pk).first()
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


# Free-form per-athlete override cells, mapped to their model ``max_length``.
OVERRIDE_TEXT_FIELDS = {"swap": 255, "sets": 32, "reps": 32, "note": 255}


def _group_member_or_none(group, athlete_id):
    """The group's *active* membership for ``athlete_id`` (a User UUID), or None.

    Scoped to the group's own coach + active links so an override can only ever
    target a current member; a malformed id, a stranger, or an ended member all
    resolve to None (the endpoint answers 400).
    """
    if not isinstance(athlete_id, str) or not athlete_id:
        return None
    try:
        return (
            GroupMembership.objects.filter(
                group=group,
                relationship__athlete_id=athlete_id,
                relationship__coach=group.coach,
                relationship__status=CoachAthlete.Status.ACTIVE,
            )
            .select_related("relationship__athlete")
            .first()
        )
    except (ValueError, ValidationError):
        return None  # athlete_id wasn't a valid UUID


def _clean_override_diff(payload, existing):
    """Validate the posted override fields onto ``existing``, or return a 400.

    Returns ``(diff, None)`` on success or ``(None, HttpResponseBadRequest)``.
    **Merge** semantics, matching the autosave ``prescription_patch`` convention:
    a field *absent* from the payload keeps its current value (from ``existing``,
    or empty/None when creating), so a partial update never silently drops the
    other parts of a multi-field adjust. A field *present* overwrites — send it
    empty (``"swap": ""`` / ``"load_pct": null``) to clear just that part.
    ``swap``/``sets``/``reps``/``note`` are free text within their model lengths;
    ``load_pct`` is an integer in the model's sane band (or null).
    """
    diff = {
        "swap_name": existing.swap_name if existing else "",
        "sets": existing.sets if existing else "",
        "reps": existing.reps if existing else "",
        "note": existing.note if existing else "",
        "load_pct": existing.load_pct if existing else None,
    }
    for field, max_length in OVERRIDE_TEXT_FIELDS.items():
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            return None, HttpResponseBadRequest(f"{field} must be a string.")
        if len(value) > max_length:
            return None, HttpResponseBadRequest(f"{field} is too long.")
        diff["swap_name" if field == "swap" else field] = value
    if "load_pct" in payload:
        load_pct = payload["load_pct"]
        if load_pct is not None and (
            not isinstance(load_pct, int)
            or isinstance(load_pct, bool)
            or not (
                PrescriptionOverride.MIN_LOAD_PCT
                <= load_pct
                <= PrescriptionOverride.MAX_LOAD_PCT
            )
        ):
            return None, HttpResponseBadRequest(
                f"load_pct must be an integer between "
                f"{PrescriptionOverride.MIN_LOAD_PCT} and "
                f"{PrescriptionOverride.MAX_LOAD_PCT}."
            )
        diff["load_pct"] = load_pct
    return diff, None


def _override_response(plan, prescription):
    """The override endpoint's reply: the row + its (recomputed) adj badge.

    ``adj`` reflects *all* the row's remaining active-member adjusts, so the
    front-end can repaint the badge after a set or a clear (it may still be lit by
    another member, or now dark).
    """
    entry = group_adjustments(plan, [prescription]).get(prescription.pk)
    return JsonResponse(
        {
            "ok": True,
            "prescription": serialize_prescription(prescription),
            "adj": entry["adj"] if entry else None,
            "adjusts": entry["adjusts"] if entry else [],
            # Row-level reply + refreshed history (see prescription_patch).
            "history": serialize_plan_history(plan),
        }
    )


@login_required
@require_POST
def prescription_override(request, plan_id, pk):
    """Set or clear one member's per-athlete adjust on a shared-program row (Phase 3).

    Group plans only — the adjust overlay layers on a *shared* program, so an
    individual plan is a 400. Coach-scoped via ``_coach_plan_or_forbidden`` (403
    if not the group's coach); the prescription must belong to the plan (404
    otherwise) and ``athlete`` must be an active member of the group (400). Body:
    ``{"athlete": <uuid>, "swap"/"load_pct"/"sets"/"reps"/"note"}`` to set, or
    ``{"athlete": <uuid>, "clear": true}`` to drop the whole adjust. Field updates
    **merge** (an omitted field keeps its current value, like the autosave
    ``prescription_patch``); send a field empty to clear just that part, and an
    adjust left with no parts is removed. Fully validated before any write.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    if not plan.is_group:
        return HttpResponseBadRequest("Overrides apply to a group's shared program.")
    prescription = _cell_or_404(plan, pk)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Malformed JSON.")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("Expected a JSON object.")

    membership = _group_member_or_none(plan.group, payload.get("athlete"))
    if membership is None:
        return HttpResponseBadRequest("athlete must be an active member of the group.")

    if payload.get("clear"):
        with transaction.atomic():
            record_plan_action(plan, "Edited override")
            membership.clear_override(prescription)
            _touch_plan(plan)
        return _override_response(plan, prescription)

    existing = membership.overrides.filter(prescription=prescription).first()
    diff, error = _clean_override_diff(payload, existing)
    if error is not None:
        return error
    try:
        with transaction.atomic():
            record_plan_action(plan, "Edited override")
            membership.set_override(prescription, **diff)
            _touch_plan(plan)
    except InvalidTransition:
        # The prescription is scoped to this plan above, so its group always
        # matches the membership; this stays defensive against future drift.
        return HttpResponseBadRequest("The prescription is not in this program.")
    return _override_response(plan, prescription)


def _live_session_in_plan_or_none(plan, session_id):
    """A live ``Session`` of ``plan`` by pk, or ``None`` (a bad body reference).

    Mirrors ``_group_member_or_none``'s convention: a body-referenced id that
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
    a 400 (``_live_session_in_plan_or_none``, mirroring ``prescription_override``'s
    ``_group_member_or_none`` bad-reference convention) rather than a 404 — only
    the URL-segment ``pk`` gets the 404 treatment.
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
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
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
        _touch_plan(plan)
    return JsonResponse({"ok": True, "history": serialize_plan_history(plan)})


@login_required
@require_POST
def prescription_swap(request, plan_id, pk):
    """Set or clear a cell's one-week free-text swap (P2 exceptions, issue #440).

    Body ``{"swap_name": "<str>"}`` sets a free-text substitute for just this
    week (``.strip()``-ed; blank after stripping clears, same as
    ``{"clear": true}``); over 255 chars is a 400. ``{"clear": true}`` clears
    it outright. Either way ``swap_exercise`` is cleared — a catalog-linked
    swap (``swap_exercise_id``) is a DEFERRED follow-up, not this endpoint.

    TODO(P?): support a catalog ``swap_exercise_id`` swap alongside this
    free-text one.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    cell = _cell_or_404(plan, pk)
    payload, bad = _json_object_body(request)
    if bad is not None:
        return bad

    clear = payload.get("clear") is True
    name = ""
    if not clear:
        if "swap_name" not in payload:
            return JsonResponse(
                {"ok": False, "error": "swap_name or clear is required."}, status=400
            )
        raw_name = payload["swap_name"]
        if not isinstance(raw_name, str):
            return JsonResponse(
                {"ok": False, "error": "swap_name must be a string."}, status=400
            )
        if len(raw_name) > 255:
            return JsonResponse(
                {"ok": False, "error": "swap_name is too long."}, status=400
            )
        name = raw_name.strip()
        if not name:
            clear = True  # blank after stripping clears, same as {"clear": true}

    # The block-shared slot's name, not ``cell.name`` — that resolves THROUGH
    # the very swap this label is describing setting/clearing.
    slot_name = cell.exercise_slot.name
    with transaction.atomic():
        if clear:
            record_plan_action(plan, f"Cleared swap on {slot_name}")
            cell.swap_name = ""
            cell.swap_exercise = None
        else:
            record_plan_action(plan, f"Swapped {slot_name} → {name}")
            cell.swap_name = name
            cell.swap_exercise = None
        cell.save(update_fields=["swap_name", "swap_exercise"])
        _touch_plan(plan)
    return JsonResponse({"ok": True, "history": serialize_plan_history(plan)})


# Numeric fields "fill across weeks" copies — mirrors PATCHABLE_FIELDS minus
# ``load_type`` (an enum, not free text, but still a per-week number setting).
_FILL_FIELDS = ["sets", "reps", "load", "load_type", "rpe", "rest", "note"]


@login_required
@require_POST
def prescription_fill(request, plan_id, pk):
    """Copy a cell's numeric fields to sibling weeks of the same row (P2, #440).

    Body OPTIONAL ``{"week_ids": [<int>...]}`` — the target weeks; absent or
    empty means every OTHER live week of this cell's ``exercise_slot``. Copies
    only ``sets/reps/load/load_type/rpe/rest/note`` — never a target's
    ``skipped``/``swap_*``, which stay whatever one-week exception they were.
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

    targets = Prescription.objects.filter(
        exercise_slot_id=cell.exercise_slot_id, week__deleted_at__isnull=True
    ).exclude(week_id=cell.week_id)
    if week_ids:
        targets = targets.filter(week_id__in=week_ids)
    targets = list(targets)

    with transaction.atomic():
        record_plan_action(plan, f"Filled {cell.name} across weeks")
        for target in targets:
            for field in _FILL_FIELDS:
                setattr(target, field, getattr(cell, field))
            target.save(update_fields=_FILL_FIELDS)
        _touch_plan(plan)
    return JsonResponse(
        {"ok": True, "filled": len(targets), "history": serialize_plan_history(plan)}
    )


@login_required
@require_POST
def coach_set_one_rm(request, plan_id, pk):
    """Set or clear an athlete's 1RM from the designer's %1RM badge (1RM Phase 3).

    The coach-side companion to ``athlete_set_one_rm``: a coach prescribing a
    %1RM target needs the athlete's max for it to mean anything, so they can set
    it here directly — useful before the athlete has ever logged the lift.
    Individual plans only (a group plan has no single athlete → 400). Coach-scoped
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
    if plan.is_group:
        return HttpResponseBadRequest("A 1RM belongs to a single athlete, not a group.")
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


def _fan_out_group_delivery(request, plan):
    """Deliver a group plan's current week to every active member (groups Phase 4).

    Runs the model fan-out (``MesoGroup.deliver_current_week``) — each member's
    *resolved* week materialized + stamped + snapshotted — then notifies each
    athlete (email + push, best-effort on commit), reusing the individual deliver
    hook. Returns ``(summary, error)``: ``error`` is a human message when there is
    nothing to deliver (no week / no members), which the callers map to a 400 /
    flashed error. The whole fan-out runs inside the request's transaction
    (``ATOMIC_REQUESTS``), so a partial fan-out can't half-commit.
    """
    try:
        # Deliver the *requested* plan, not whichever the group reselects, so a
        # group holding more than one program can't drift to a different one.
        now, delivered = plan.group.deliver_current_week(plan)
    except InvalidTransition as exc:
        return None, str(exc)
    for member_plan, member_week in delivered:
        _notify_athlete_delivered(request, member_plan, member_week)
    return {"members": len(delivered), "delivered_at": now.isoformat()}, None


@login_required
@require_POST
def plan_deliver(request, plan_id):
    """Deliver a **block** of the plan: stamp + snapshot its whole mesocycle (P3).

    The individual deliver path releases the whole block (one ``Mesocycle``, all
    its live weeks) at once, not one week at a time. The target week is resolved
    exactly as before — the ``week_id`` in the body (the multi-week designer's
    "send the week I'm viewing"), else the plan's **current** (live) week — but it
    only *selects which block* to send: every live week of ``target.mesocycle`` is
    stamped ``delivered_at`` (one shared timestamp) and snapshotted. The chosen
    week must belong to the plan (a foreign week is a 404). Re-delivering re-stamps
    every week and writes fresh ``WeekDelivery`` rows. Delivering never changes
    ``is_current`` — releasing a block doesn't move the live pointer. A group plan
    still fans out its current week per-member (per-week delivery is a group-path
    affordance a later phase rewrites), so its branch is unchanged.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    if plan.is_group:
        # A group plan fans its current week out to every active member (each
        # member's *resolved* program), groups Phase 4. ``week_id`` is ignored —
        # the group always sends its current week.
        summary, error = _fan_out_group_delivery(request, plan)
        if error is not None:
            return HttpResponseBadRequest(error)
        return JsonResponse({"ok": True, **summary}, status=201)
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
    # #441 P3-5: the deliver step auto-advances the moment the block is
    # delivered. A no-op unless the coach is parked on deliver.
    meso_tour.advance_if_on_step(request.user, "deliver")
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "mesocycle": {"id": block.pk, "name": block.name},
            "week_count": len(live_weeks),
        },
        status=201,
    )


def _notify_athlete_delivered(request, plan, week):
    """Best-effort: email **and** push the athlete that ``week`` was delivered.

    S3 (email, Phase 4a) + S7 (web push, Phase 4b). Deferred to
    ``transaction.on_commit`` so it fires only after the delivery actually
    commits — under ``ATOMIC_REQUESTS`` the view runs in a transaction, and a
    rolled-back deliver must not notify a false "your week is ready". Each
    channel is independently best-effort: a failure in one is swallowed and
    logged, never a 500 or a rolled-back deliver, and never blocks the other.

    Sandbox gate (S4): a sandbox coach's deliveries never notify — there is no
    real person behind a seeded demo athlete.
    """
    if meso_sandbox.is_sandbox(plan.coach):
        return
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
                send_week_delivered_email(
                    athlete=plan.athlete,
                    coach=plan.coach,
                    plan=plan,
                    week=week,
                    home_url=home_url,
                    unsubscribe_url=unsubscribe_url,
                )
        except Exception:  # mail is best-effort; never fail a delivery on it
            logger.exception(
                "Failed to send delivery email for plan %s week %s",
                plan.pk,
                week.pk,
            )
        try:
            meso_push.notify_week_delivered(
                athlete=plan.athlete,
                coach=plan.coach,
                plan=plan,
                week=week,
                home_url=home_url,
            )
        except Exception:  # push is best-effort too; never fail a delivery on it
            logger.exception(
                "Failed to send delivery push for plan %s week %s",
                plan.pk,
                week.pk,
            )

    transaction.on_commit(_send)


def _notify_athlete_block_delivered(request, plan, mesocycle, week_count):
    """Best-effort: ONE email + ONE push that a whole **block** was delivered.

    The block-level peer of ``_notify_athlete_delivered`` (P3): the individual
    deliver path releases the whole mesocycle at once, so the athlete gets a
    single "your new block is ready" nudge — not one notification per week. Same
    contract as the per-week notifier: sandbox-gated at the coach check, deferred
    to ``transaction.on_commit`` (under ``ATOMIC_REQUESTS`` a rolled-back deliver
    must not notify a false "your block is ready"), and each channel is
    independently best-effort — a failure in one is swallowed and logged, never a
    500 or a rolled-back deliver, and never blocks the other.

    Sandbox gate (S4): a sandbox coach's deliveries never notify — there is no
    real person behind a seeded demo athlete.
    """
    if meso_sandbox.is_sandbox(plan.coach):
        return
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
    # transaction must be *explicit*: the project's module-level ``ATOMIC_REQUESTS``
    # is inert (Django reads it per-entry from ``DATABASES``, which ``dj_database_url``
    # doesn't set), so without this ``select_for_update`` would raise in autocommit
    # on Postgres and the count-then-create gate would be racy. (On SQLite/tests the
    # lock is a no-op; the real serialization is on Postgres in prod.) Early returns
    # below just commit an empty transaction — nothing is written on those paths.
    with transaction.atomic():
        User.objects.select_for_update().filter(pk=request.user.pk).first()
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

        # Guard on the key here so we answer 503 without persisting a dead batch.
        if agent_client.get_default_client() is None:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "The Meso agent is not configured (no API key).",
                },
                status=503,
            )

        # A group run grounds on the group (members + folded contraindications)
        # and edits the shared program (groups Phase 1); tag it ``group`` for the
        # usage ledger (attributed to the group, athlete null).
        trigger = (
            AgentProposalBatch.Trigger.GROUP
            if plan.is_group
            else AgentProposalBatch.Trigger.MANUAL
        )
        batch = agent_service.create_drafting_batch(
            plan, instruction, coach=request.user, trigger=trigger
        )
    # The batch is committed; enqueue the worker run and bump the plan outside the
    # lock so neither holds the coach row.
    agent_jobs.dispatch_proposal(batch.pk)
    _touch_plan(plan)
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
        changes = [
            serialize_proposed_change(c)
            for c in batch.changes.select_related("membership__relationship__athlete")
        ]
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

    Scoped to a plan the coach may *edit* (``editable_by``), so it covers an
    individual plan over an active relationship **and** a group plan the coach owns
    (the group agent runs behind this same review gate) — a foreign/unknown batch
    is a 404.
    """
    batch = (
        AgentProposalBatch.objects.filter(
            pk=batch_id, plan__in=Plan.objects.editable_by(request.user)
        )
        .select_related("plan", "plan__relationship", "plan__group")
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
        record_plan_action(batch.plan, "Applied agent changes")
        result = agent_apply.apply_batch(batch)
    # Where the review screen sends the coach next. An individual plan has a
    # deliver screen; a group plan's delivery is deliver-to-all (no individual
    # deliver screen — ``DeliverView`` is ``for_coach`` individual-only and would
    # 404 a group plan), so a group batch lands back in the designer where the
    # shared program + its group delivery live.
    if batch.plan.is_group:
        next_url = reverse("meso:designer_plan", kwargs={"plan_id": batch.plan_id})
    else:
        next_url = reverse("meso:deliver_plan", kwargs={"plan_id": batch.plan_id})
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
    # Don't open a second Checkout for a coach who already has a live Stripe
    # subscription — completing it would create a duplicate (double-billing).
    # They manage the existing one in the Portal; a canceled mirror re-subscribes
    # freely.
    sub = getattr(request.user, "coach_subscription", None)
    if (
        sub
        and sub.stripe_subscription_id
        and sub.status
        in (CoachSubscription.Status.ACTIVE, CoachSubscription.Status.PAST_DUE)
    ):
        messages.info(
            request,
            "You already have a subscription — manage it in the billing portal.",
        )
        return redirect("meso:roster")
    roster_url = request.build_absolute_uri(reverse("meso:roster"))
    try:
        session = billing_gateway.create_subscription_checkout_session(
            request.user,
            success_url=f"{roster_url}?billing=success",
            cancel_url=f"{roster_url}?billing=cancel",
        )
    except Exception:  # noqa: BLE001 — surface a friendly error, never a 500
        logger.exception("Stripe checkout session failed for coach %s", request.user.pk)
        messages.error(request, "Could not start checkout. Please try again.")
        return redirect("meso:roster")
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
            .select_related("plan", "plan__relationship__athlete", "plan__group")
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
