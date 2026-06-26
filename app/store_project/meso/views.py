from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


class MesoDesignerView(LoginRequiredMixin, TemplateView):
    """The Meso AI strength-training program designer.

    A self-contained, full-screen coach tool. All program/agent state lives
    client-side (Alpine.js); the view only serves the shell. Wiring it to real
    athlete profiles, logged sets, and a live agent is a future step.
    """

    template_name = "meso/designer.html"
