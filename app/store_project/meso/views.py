from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from . import mockdata
from . import presenters
from .models import CoachAthlete


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso AI strength-training program designer.

    A self-contained, full-screen coach tool. All program/agent state lives
    client-side (Alpine.js); the view only serves the shell. Wiring it to real
    athlete profiles, logged sets, and a live agent is a future step (Phase 3).
    """

    template_name = "meso/designer.html"


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
    """Confirm what gets sent to the athlete, when, and how."""

    template_name = "meso/deliver.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["active"] = "designer"
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
