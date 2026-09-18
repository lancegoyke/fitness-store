"""The staff-gated email deliverability dashboard (issue #507 part 2).

Reads the SES event ledger ``presenters.email_dashboard`` aggregates:
``SentEmail`` (written the moment ``SESBackend`` hands a message to SES) and
``EmailEvent`` (one row per SES send/delivery/open/click/bounce/complaint
event, matched back to the ``SentEmail`` it belongs to when possible — see
``ses_events`` and ``models``).

Gate + window handling mirror ``meso.views.TourFunnelView`` /
``UsageDashboardView`` exactly: anonymous → login redirect (the
``UserPassesTestMixin`` default), authenticated non-staff → a flat 403 (so a
logged-in coach or athlete can't probe org-wide delivery data), staff → 200.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.generic import View
from django_ses.models import BlacklistedEmail

from . import presenters

DEFAULT_DAYS = 30
VALID_DAYS = (7, 30, 90)


class EmailDashboardView(UserPassesTestMixin, TemplateView):
    """Owner-facing SES deliverability dashboard.

    ``?days=7|30|90`` picks the report window (default 30; anything else
    degrades to 30 with a flashed warning, mirroring
    ``UsageDashboardView._window`` — the page always renders rather than
    erroring on a hand-edited query string). ``?q=<email>`` additionally
    surfaces one recipient's full send/event history via
    ``presenters.email_dashboard``'s ``recipient_query``.
    """

    template_name = "notifications/email_dashboard.html"

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
        days = self._days()
        since = timezone.now() - datetime.timedelta(days=days)
        q = (self.request.GET.get("q") or "").strip()
        ctx["active"] = "email"
        ctx["days"] = days
        ctx["q"] = q
        ctx.update(presenters.email_dashboard(since=since, recipient_query=q))
        return ctx

    def _days(self):
        """The report window (in days) from ``?days=``; 30 on bad/missing input."""
        raw = self.request.GET.get("days")
        if raw:
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                parsed = None
            if parsed in VALID_DAYS:
                return parsed
            messages.error(
                self.request,
                f"Ignoring invalid days {raw!r}; showing the last {DEFAULT_DAYS} days.",
            )
        return DEFAULT_DAYS


class EmailDashboardBlacklistClearView(UserPassesTestMixin, View):
    """Remove one recipient from the SES blacklist (POST only).

    A bounce or complaint auto-blacklists a recipient (django-ses's own
    signal handlers, connected off ``bounce_received``/``complaint_received``);
    once the underlying problem is fixed, staff need a one-click way to let
    SES try that recipient again. Gate mirrors ``EmailDashboardView``; a GET
    is simply not allowed (``http_method_names`` limits this to POST, so
    Django's own ``View.dispatch`` 405s it).
    """

    http_method_names = ["post"]

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied
        return super().handle_no_permission()

    def post(self, request, pk):
        entry = get_object_or_404(BlacklistedEmail, pk=pk)
        entry.delete()
        messages.success(request, f"Removed {entry.email} from the SES blacklist.")
        return redirect(reverse("notifications:email_dashboard"))
