from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.views.generic import TemplateView

from . import mockdata


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso AI strength-training program designer.

    A self-contained, full-screen coach tool. All program/agent state lives
    client-side (Alpine.js); the view only serves the shell. Wiring it to real
    athlete profiles, logged sets, and a live agent is a future step.
    """

    template_name = "meso/designer.html"


class RosterView(LoginRequiredMixin, TemplateView):
    """Front door: the coach's athletes, groups, and recent activity."""

    template_name = "meso/roster.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        amap = mockdata.athletes_map()
        ctx["active"] = "roster"
        ctx["athletes"] = mockdata.ATHLETES
        ctx["groups"] = [
            {**g, "member_objs": [amap[s] for s in g["members"] if s in amap]}
            for g in mockdata.GROUPS
        ]
        ctx["activity"] = [
            {**ev, "athlete": amap.get(ev["who"])} for ev in mockdata.ACTIVITY
        ]
        ctx["needs_review"] = sum(
            1 for a in mockdata.ATHLETES if a["status"] == "needs_review"
        )
        return ctx


class AthleteProfileView(LoginRequiredMixin, TemplateView):
    """Full athlete record — the expanded version of the designer's left rail."""

    template_name = "meso/athlete_profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        athlete = mockdata.athlete_by_slug(kwargs.get("slug"))
        if athlete is None:
            raise Http404("Unknown athlete")
        ctx["active"] = "roster"
        ctx["athlete"] = athlete
        ctx["coach_style"] = mockdata.COACH_STYLE
        ctx["macrocycle"] = mockdata.MACROCYCLE
        ctx["results_summary"] = mockdata.RESULTS_SUMMARY
        return ctx


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
