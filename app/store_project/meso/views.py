import datetime
import ipaddress
import json
import logging
import uuid
from urllib.parse import urlparse

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
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
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.emails import send_week_delivered_email

from . import demo as meso_demo
from . import one_rm as meso_one_rm
from . import presenters
from . import push as meso_push
from .agent import apply as agent_apply
from .agent import client as agent_client
from .agent import jobs as agent_jobs
from .agent import service as agent_service
from .billing import access as billing_access
from .billing import agent_usage_report as usage_report
from .billing import seats as billing_seats
from .billing import stripe_gateway as billing_gateway
from .billing import webhooks as billing_webhooks
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachProfile
from .models import CoachSubscription
from .models import ExercisePrescription
from .models import GroupMembership
from .models import InvalidTransition
from .models import LoadType
from .models import LoggedSet
from .models import Mesocycle
from .models import MesoGroup
from .models import Plan
from .models import PrescriptionOverride
from .models import ProposedChange
from .models import PushSubscription
from .models import Session
from .models import SessionLog
from .models import Week
from .models import WeekDelivery
from .serializers import current_week
from .serializers import group_adjustments
from .serializers import serialize_chat_thread
from .serializers import serialize_plan
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
        .prefetch_related("prescriptions")
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
                {"athlete_next": reverse("meso:athlete_home")},
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
            .prefetch_related("athlete__contraindications")
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
                has_working_plan=link.pk in have_plan,
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
        # auto-adjusts land in groups Phase 2/3. Activity (Phase 3) needs logged
        # sessions; needs-review (Phase 2) needs agent state. Empty until then.
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
        ctx["activity"] = []
        ctx["needs_review"] = 0
        # First-run UX (Phase 2): a fresh coach with nothing yet gets an
        # onboarding card that teaches the model and offers the one-click demo;
        # once demo data is loaded a banner offers to remove it (Q3).
        ctx["has_demo"] = meso_demo.has_demo(self.request.user)
        ctx["is_empty"] = not athletes and not ctx["groups"]
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
        ctx["active"] = "roster"
        ctx["athlete"] = presenters.profile_athlete(link.athlete)
        ctx["coach_style"] = presenters.coach_style(self.request.user)
        # The relationship's working program (first-time-UX Phase 1): when one
        # exists the CTAs open it in the designer; when not, they create one.
        ctx["working_plan"] = link.working_plan()
        # Whether to offer "Draft with AI" on the create CTA — the same agent
        # allowance gate the endpoint enforces (the draft *is* an agent run).
        ctx["can_use_agent"] = billing_access.can_use_agent(self.request.user)
        # Current block / macrocycle / latest results arrive with the program
        # schema (Phase 2) and logging (Phase 3).
        ctx["macrocycle"] = []
        ctx["results_summary"] = None
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
    """
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
    return redirect("meso:designer_plan", plan_id=plan.pk)


@login_required
@require_POST
def demo_load(request):
    """Load a coach-scoped demo workspace so a new coach can explore (Q3, Phase 2).

    A populated, **clearly-labeled, fully-removable** workspace — five athletes, a
    built/delivered/logged program, and a group — scoped to this coach, idempotent,
    billing-neutral, and silent (no demo-athlete email/push). Lands on the roster
    where the data now shows, with a "Remove demo data" affordance.
    """
    # Loading a demo is an implicit "I'm coaching now": ensure the CoachProfile
    # exists (mirrors start_coaching's free path) so demo links never make a user a
    # coach via a side door without one — keeping coach state consistent.
    CoachProfile.objects.get_or_create(user=request.user)
    meso_demo.load_demo(request.user)
    messages.success(
        request,
        "Demo data loaded — explore a populated workspace. Remove it any time.",
    )
    return redirect("meso:roster")


@login_required
@require_POST
def demo_clear(request):
    """Remove exactly this coach's demo data (never their real data) — the teardown."""
    meso_demo.clear_demo(request.user)
    messages.success(request, "Demo data removed.")
    return redirect("meso:roster")


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
    """
    session = (
        Session.objects.filter(
            pk=pk,
            week__delivered_at__isnull=False,
            week__mesocycle__plan__in=_athlete_plans(user),
        )
        .select_related("week__mesocycle__plan__relationship")
        .prefetch_related("prescriptions")
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
        log.sets.all().delete()
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
            list(session.prescriptions.all()),
            session.week.mesocycle.plan.unit,
        )
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
    prescriptions = {p.pk: p for p in session.prescriptions.all()}
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
    allowed_ids = {p.pk for p in session.prescriptions.all()}
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

PWA_THEME_COLOR = "#3c73c5"  # meso accent (oklch(0.56 0.14 258))
PWA_BACKGROUND_COLOR = "#f5f6f7"  # meso app background (--bg)


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
PWA_CACHE_VERSION = "meso-pwa-v2"


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
    # A new active link is a billable seat — best-effort sync the coach's Stripe
    # quantity (no-op unless they're paid; the daily sweep is the backstop).
    billing_seats.schedule_seat_sync(link.coach)
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
    # The seat is freed — best-effort sync the coach's Stripe quantity down.
    billing_seats.schedule_seat_sync(link.coach)
    messages.success(request, "Relationship ended.")
    return redirect("meso:roster")


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
    """
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
    if coach is None:
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
# act on an existing CoachAthlete between two Users. See docs/meso/invites-plan.md.


@login_required
@require_POST
def coach_invite(request):
    """Coach invites an athlete by email → a pending ``CoachInvite`` + claim email.

    A plain form POST from the roster's "Invite an athlete" disclosure. The email
    is validated and normalized; a coach cannot invite their own address; a
    re-invite reuses the open pending row (``open_for``). The claim email is sent
    on ``transaction.on_commit`` and is best-effort — a mail backend failure is
    logged, never a 500 or a lost invite. Always lands back on the roster.
    """
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
    """
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
    """
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
                # Claiming materializes an active link — a billable seat for the
                # coach; best-effort sync their Stripe quantity (daily sweep backstop).
                billing_seats.schedule_seat_sync(invite.coach)
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
PATCHABLE_FIELDS = {
    "name": 255,
    "sets": 32,
    "reps": 32,
    "load": 32,
    "rpe": 32,
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


@login_required
@require_POST
def prescription_patch(request, plan_id, pk):
    """Patch one prescription cell (or a small batch of cells)."""
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    prescription = get_object_or_404(
        ExercisePrescription, pk=pk, session__week__mesocycle__plan=plan
    )
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

    if updates:
        for field, value in updates.items():
            setattr(prescription, field, value)
        prescription.save(update_fields=list(updates))
        _touch_plan(plan)
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}
    )


@login_required
@require_POST
def session_add_exercise(request, plan_id, pk):
    """Append a blank prescription row to a session."""
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    session = get_object_or_404(Session, pk=pk, week__mesocycle__plan=plan)
    next_order = (session.prescriptions.aggregate(m=Max("order"))["m"] or 0) + 1
    prescription = ExercisePrescription.objects.create(
        session=session,
        name="New exercise",
        order=next_order,
        sets="3",
        reps="10",
        load="",
        rpe="7",
        note="",
    )
    _touch_plan(plan)
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}, status=201
    )


@login_required
@require_POST
def session_add(request, plan_id):
    """Append a blank training day (with a starter row) to a week of the plan.

    "Add a day" adds a ``Session`` to the week the designer is showing. An optional
    ``week_id`` in the body pins that week — the multi-week switcher can open a week
    other than the live one, and the day must land where the coach is looking (else
    a reload shows it on the wrong week). It defaults to ``current_week`` for the
    first-time-UX caller that predates the switcher. The week is scoped to the plan
    (a foreign week is a 404). Scoped + edit-gated like the other designer writes via
    ``_editable_plan_or_response`` (403 foreign, 402 over-limit). Returns the new day
    in the grid's day shape so the client can append it without a reload.
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
        week = get_object_or_404(Week, pk=week_id, mesocycle__plan=plan)
    else:
        week = current_week(plan)
    if week is None:
        return HttpResponseBadRequest("This plan has no week to add a day to.")
    # Allocate the next day_number/order under a row lock on the week so a
    # double-click or two concurrent submits can't read the same max and create
    # duplicate "Day N" rows (Session has no uniqueness on these). The explicit
    # transaction is required: prod views run in autocommit (ATOMIC_REQUESTS is
    # inert here), so the lock must own its own transaction to be held.
    with transaction.atomic():
        Week.objects.select_for_update().filter(pk=week.pk).first()
        next_order = (week.sessions.aggregate(m=Max("order"))["m"] or 0) + 1
        next_day = (week.sessions.aggregate(m=Max("day_number"))["m"] or 0) + 1
        session = Session.objects.create(
            week=week, day_number=next_day, name=f"Day {next_day}", order=next_order
        )
        ExercisePrescription.objects.create(
            session=session, name="New exercise", order=0, sets="3", reps="10", rpe="7"
        )
        _touch_plan(plan)
    return JsonResponse({"ok": True, "session": serialize_session(session)}, status=201)


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
    week = get_object_or_404(Week, pk=week_id, mesocycle__plan=plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=week)})


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
        Mesocycle.objects.select_for_update().filter(pk=mesocycle.pk).first()
        new_week = mesocycle.append_week()
        _touch_plan(plan)
    return JsonResponse({"ok": True, **serialize_plan(plan, week=new_week)}, status=201)


@login_required
@require_POST
def week_set_current(request, plan_id, week_id):
    """Make ``week`` the plan's current week — its designer-default + deliver target.

    The designer's "Make current": flips the live pointer to the viewed week so
    delivery (which sends ``current_week``) targets it and the designer opens onto
    it next time. Exactly one week is current — the others in the plan are cleared.
    Scoped + edit-gated (403 foreign, 402 over-limit); a foreign week is a 404.
    Row-locks the plan so concurrent set-currents serialize. Returns the plan
    pinned to the new current week.
    """
    plan, forbidden = _editable_plan_or_response(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = get_object_or_404(Week, pk=week_id, mesocycle__plan=plan)
    with transaction.atomic():
        Plan.objects.select_for_update().filter(pk=plan.pk).first()
        Week.objects.filter(mesocycle__plan=plan).exclude(pk=week.pk).update(
            is_current=False
        )
        if not week.is_current:
            week.is_current = True
            week.save(update_fields=["is_current"])
        _touch_plan(plan)
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
    prescription = get_object_or_404(
        ExercisePrescription, pk=pk, session__week__mesocycle__plan=plan
    )
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
        membership.clear_override(prescription)
        _touch_plan(plan)
        return _override_response(plan, prescription)

    existing = membership.overrides.filter(prescription=prescription).first()
    diff, error = _clean_override_diff(payload, existing)
    if error is not None:
        return error
    try:
        membership.set_override(prescription, **diff)
    except InvalidTransition:
        # The prescription is scoped to this plan above, so its group always
        # matches the membership; this stays defensive against future drift.
        return HttpResponseBadRequest("The prescription is not in this program.")
    _touch_plan(plan)
    return _override_response(plan, prescription)


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
    prescription = get_object_or_404(
        ExercisePrescription, pk=pk, session__week__mesocycle__plan=plan
    )
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
    """Deliver a week of the plan: stamp ``delivered_at`` + snapshot it (Phase 4).

    Delivers the plan's **current** (live) week by default, or a specific week
    when the body carries a ``week_id`` — the multi-week designer's "send the week
    I'm viewing". A coach can deliver a built-ahead week directly: delivering never
    changes ``is_current``, so sending a future week doesn't move the live pointer.
    Visibility is by newest ``delivered_at`` (``latest_delivered_week``), so the
    athlete lands on the week just sent while the live pointer stays put. The chosen
    week must belong to the plan (a foreign week is a 404). A group plan ignores
    ``week_id`` and fans out its current week (per-week delivery is an
    individual-designer affordance).
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
    # An empty body (the bare deliver button) means "no week_id" — deliver the
    # live week, as before; a present-but-malformed body is a 400, not a silent
    # delivery of the wrong week.
    week_id, bad = _body_week_id(request)
    if bad is not None:
        return bad
    if week_id is not None:
        week = get_object_or_404(Week, pk=week_id, mesocycle__plan=plan)
    else:
        week = current_week(plan)
    if week is None:
        return HttpResponseBadRequest("This plan has no week to deliver.")
    now = timezone.now()
    week.delivered_at = now
    week.save(update_fields=["delivered_at"])
    WeekDelivery.objects.create(
        week=week, delivered_at=now, payload=serialize_week_snapshot(week)
    )
    _touch_plan(plan)
    _notify_athlete_delivered(request, plan, week)
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "week": {"id": week.pk, "label": f"Wk {week.index}"},
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
    """
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
        # Agent gate (D4): the Claude agent has real per-call cost. An
        # active/trial/comped coach is unlimited; a free coach gets
        # ``FREE_AGENT_ALLOWANCE`` runs/month and then 402s (the designer shows the
        # upgrade CTA in place of the composer once exhausted). Only a free coach
        # reaches this branch, so the copy is the allowance-used-up message.
        # Defended here, not just in the UI, because the API cost is real.
        if not billing_access.can_use_agent(request.user):
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        f"You've used all {CoachSubscription.FREE_AGENT_ALLOWANCE} "
                        "free agent runs this month. Start your free trial or "
                        "subscribe for unlimited agent runs."
                    ),
                    "upgrade": True,
                },
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
    # Base + per-seat billing (D13) needs *both* Prices configured; ship dormant
    # (bounce gracefully) until the owner creates both, so we never half-charge.
    if not settings.MESO_SEAT_PRICE_ID or not settings.MESO_BASE_PRICE_ID:
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
    """
    if not _is_coach(request.user):
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

        Resolves ``?week=`` to a week of this plan, or None (the presenter falls
        back to the live week). A missing / foreign / non-numeric ``week`` is
        ignored rather than a 404: the confirm screen always renders something
        deliverable, and the deliver POST itself validates the chosen week strictly.
        """
        raw = self.request.GET.get("week")
        if not raw:
            return None
        try:
            week_id = int(raw)
        except (TypeError, ValueError):
            return None
        return Week.objects.filter(pk=week_id, mesocycle__plan=plan).first()


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
