import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Max
from django.http import Http404
from django.http import HttpResponseBadRequest
from django.http import HttpResponseForbidden
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from . import mockdata
from . import presenters
from .models import CoachAthlete
from .models import ExercisePrescription
from .models import Plan
from .models import Session
from .models import WeekDelivery
from .serializers import current_week
from .serializers import serialize_plan
from .serializers import serialize_prescription
from .serializers import serialize_week_snapshot


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso strength-training program designer.

    A self-contained, full-screen coach tool. With a ``plan_id`` the view
    serializes a real, owned plan into the page and the Alpine front-end
    hydrates from it (then autosaves edits to the API endpoints below); without
    one it falls back to the prototype's client-side fixtures until the seed
    slice (Phase 5) retires them. The agent column stays mock until its slice.
    """

    template_name = "meso/designer.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        plan_id = kwargs.get("plan_id")
        if plan_id is not None:
            plan = Plan.objects.for_coach(self.request.user).filter(pk=plan_id).first()
            if plan is None:
                raise Http404("Unknown plan")
            ctx["plan_data"] = serialize_plan(plan)
        return ctx


class RosterView(LoginRequiredMixin, TemplateView):
    """Front door: the coach's athletes (scoped to active relationships)."""

    template_name = "meso/roster.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        links = (
            CoachAthlete.objects.for_coach(self.request.user)
            .active()
            .select_related("athlete", "athlete__athlete_profile")
            .prefetch_related("athlete__contraindications")
            .order_by("athlete__name", "athlete__email")
        )
        athletes = [presenters.roster_athlete(link.athlete) for link in links]
        ctx["active"] = "roster"
        ctx["athletes"] = athletes
        # Groups (S1) need shared programs; activity (Phase 3) needs logged
        # sessions; needs-review (Phase 2) needs agent state. Empty until then.
        ctx["groups"] = []
        ctx["activity"] = []
        ctx["needs_review"] = 0
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
        # Current block / macrocycle / latest results arrive with the program
        # schema (Phase 2) and logging (Phase 3).
        ctx["macrocycle"] = []
        ctx["results_summary"] = None
        return ctx


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

    404 when the plan does not exist; 403 when it exists but the requester is
    not its coach over an active relationship.
    """
    plan = get_object_or_404(Plan, pk=plan_id)
    if plan.relationship.coach_id != request.user.id or not plan.relationship.is_active:
        return None, HttpResponseForbidden("You do not own this plan.")
    return plan, None


@login_required
@require_POST
def prescription_patch(request, plan_id, pk):
    """Patch one prescription cell (or a small batch of cells)."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
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

    if updates:
        for field, value in updates.items():
            setattr(prescription, field, value)
        prescription.save(update_fields=list(updates))
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}
    )


@login_required
@require_POST
def session_add_exercise(request, plan_id, pk):
    """Append a blank prescription row to a session."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
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
    return JsonResponse(
        {"ok": True, "prescription": serialize_prescription(prescription)}, status=201
    )


@login_required
@require_POST
def plan_deliver(request, plan_id):
    """Deliver the plan's current week: stamp ``delivered_at`` + snapshot it (Phase 4)."""
    plan, forbidden = _coach_plan_or_forbidden(request, plan_id)
    if forbidden is not None:
        return forbidden
    week = current_week(plan)
    if week is None:
        return HttpResponseBadRequest("This plan has no week to deliver.")
    now = timezone.now()
    week.delivered_at = now
    week.save(update_fields=["delivered_at"])
    WeekDelivery.objects.create(
        week=week, delivered_at=now, payload=serialize_week_snapshot(week)
    )
    return JsonResponse(
        {
            "ok": True,
            "delivered_at": now.isoformat(),
            "week": {"id": week.pk, "label": f"Wk {week.index}"},
        },
        status=201,
    )


# -- still on fixtures until their own slices ------------------------------


class ChangeReviewView(LoginRequiredMixin, TemplateView):
    """Review the batch of edits the agent proposes before they hit the program."""

    template_name = "meso/review.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        ctx["athlete"] = mockdata.athlete_by_slug("maya")
        ctx["changes"] = mockdata.PROPOSED_CHANGES
        return ctx


class DeliverView(LoginRequiredMixin, TemplateView):
    """Confirm what gets sent to the athlete, when, and how.

    With a ``plan_id`` the screen binds to a real, owned plan: it shows that
    plan's athlete + current week and its "Deliver" button POSTs to
    ``plan_deliver`` (stamp + snapshot). Without one it falls back to the
    prototype fixtures until the seed slice (Phase 5) retires them.
    """

    template_name = "meso/deliver.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
        plan_id = kwargs.get("plan_id")
        if plan_id is not None:
            plan = Plan.objects.for_coach(self.request.user).filter(pk=plan_id).first()
            if plan is None:
                raise Http404("Unknown plan")
            ctx["plan_id"] = plan.pk
            ctx.update(presenters.deliver_screen(plan))
        else:
            ctx["deliver"] = mockdata.DELIVER
            ctx["athlete"] = mockdata.athlete_by_slug(mockdata.DELIVER["athlete"])
        return ctx


class ResultsView(LoginRequiredMixin, TemplateView):
    """Logged session results vs targets — closes the loop back to the agent."""

    template_name = "meso/results.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "roster"
        ctx["summary"] = mockdata.RESULTS_SUMMARY
        ctx["rows"] = mockdata.RESULTS_ROWS
        ctx["athlete"] = mockdata.athlete_by_slug(mockdata.RESULTS_SUMMARY["athlete"])
        return ctx
