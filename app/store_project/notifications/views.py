"""Views for issue #507: the SES/SNS event webhook and the superuser dashboard.

The dashboard (part 2) reads the SES event ledger ``presenters.email_dashboard``
aggregates: ``SentEmail`` (written the moment ``SESBackend`` hands a message
to SES) and ``EmailEvent`` (one row per SES send/delivery/open/click/bounce/
complaint event, matched back to the ``SentEmail`` it belongs to when
possible — see ``ses_events`` and ``models``).

Gate + window handling mirror ``meso.views.TourFunnelView`` /
``UsageDashboardView`` exactly: anonymous → login redirect (the
``UserPassesTestMixin`` default), authenticated non-superuser → a flat 403 (so a
logged-in coach or athlete can't probe org-wide delivery data), superuser → 200.

Issue #514 moved the dashboard's template off the Meso shell onto
``admin/base_site.html``, so its context now also carries
``admin.site.each_context`` (site header, user tools, the app-list nav
sidebar, ...) -- the same context every real admin page gets.
"""

import datetime
import json
import logging

from django.conf import settings
from django.contrib import admin
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.generic import View
from django_ses.models import BlacklistedEmail
from django_ses.views import SESEventWebhookView

from . import presenters

DEFAULT_DAYS = 30
logger = logging.getLogger(__name__)
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
        return self.request.user.is_superuser

    def handle_no_permission(self):
        # Authenticated-but-unauthorized → 403 (not a pointless login bounce);
        # anonymous → the mixin's login redirect.
        if self.request.user.is_authenticated:
            raise PermissionDenied
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(admin.site.each_context(self.request))
        days = self._days()
        since = timezone.now() - datetime.timedelta(days=days)
        q = (self.request.GET.get("q") or "").strip()
        # Drives admin/base_site.html's own <title>, breadcrumbs, and <h1> --
        # all three read this one context key, no block overrides needed.
        ctx["title"] = "Email deliverability"
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
    SES try that recipient again. This action retains its staff gate; a GET is
    simply not allowed (``http_method_names`` limits this to POST, so Django's
    own ``View.dispatch`` 405s it).
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


class ScopedSESEventWebhookView(SESEventWebhookView):
    """``SESEventWebhookView``, restricted to an allow-listed SNS topic.

    django-ses's ``verify_event_message`` verifies the SNS signature is
    genuinely Amazon's, but never checks *which* topic a message came from.
    Left unguarded, anyone with an AWS account could subscribe our webhook
    URL to their own topic and publish forged Bounce/Complaint events — each
    one creates a ``BlacklistedEmail`` row (suppressing real mail via
    ``AWS_SES_USE_BLACKLIST``) or floods ``EmailEvent`` — since a valid
    Amazon signature says only "Amazon signed this", not "this came from our
    configuration set".

    The base view's ``post()`` only calls ``verify_event_message`` at all
    when ``settings.AWS_SES_VERIFY_EVENT_SIGNATURES`` is true, so the topic
    guard can't live inside that hook (an unguarded ``post()`` would let the
    check be bypassed entirely by turning signature verification off — a
    supported configuration, e.g. local testing). Overriding ``post()``
    instead means the guard runs unconditionally, before the base view's own
    signature check, so a rejected topic never costs a certificate fetch and
    an attacker's subscription is never confirmed.

    It is also the outer safety net for exceptions raised by *other*
    receivers on the same signals — namely django-ses's own
    ``bounce_handler``/``complaint_handler`` (connected in
    ``DjangoSESConfig.ready()``), which run before this app's receivers
    because ``django_ses`` precedes ``notifications`` in ``INSTALLED_APPS``.
    Their ``_blacklist_recipients`` does ``email.lower()`` on every
    recipient and raises ``AttributeError`` for a non-string
    ``emailAddress``; ``django.dispatch.Signal.send()`` stops at the first
    receiver exception, so that would otherwise 500 before our own receivers
    even ran, and SNS would redeliver the same permanently malformed event
    for hours. Our own receivers (``ses_events``) never raise anything but a
    ``DatabaseError`` (see its module docstring), so ``_dispatch_to_base``
    below only ever needs to catch exceptions from *other* receivers.
    """

    def post(self, request, *args, **kwargs):
        try:
            notification = json.loads(request.body.decode("utf-8"))
        except ValueError:
            # Malformed JSON: let the base view produce its own 400.
            return self._dispatch_to_base(request, *args, **kwargs)

        if not isinstance(notification, dict):
            # Valid JSON that isn't an object (a list, null, a string, a
            # number, ...) has no "TopicArn" to check: .get() would raise.
            return HttpResponseBadRequest("The request body must be a JSON object.")

        topic_arn = notification.get("TopicArn")
        if topic_arn not in settings.AWS_SES_EVENT_TOPIC_ARNS:
            logger.warning(
                "Rejected SNS notification for non-allow-listed TopicArn: %s",
                topic_arn,
            )
            return HttpResponseBadRequest("Unexpected SNS topic.")

        return self._dispatch_to_base(request, *args, **kwargs)

    def _dispatch_to_base(self, request, *args, **kwargs):
        """Run the base view, turning a non-DB exception into an acked 200.

        A ``django.db.DatabaseError`` (and subclasses) propagates unchanged —
        that is the deliberate "let SNS retry" path (see ``ses_events``'s
        module docstring); ``ses_events`` already downgrades the permanent
        ``DataError``/``IntegrityError`` subclasses before they reach here,
        so what arrives is transient. Any other exception — from django-ses's
        own blacklist handlers, or anything else on the signal chain — is
        logged and answered with a 200 so SNS stops redelivering an event we
        can never successfully process.
        """
        try:
            return super().post(request, *args, **kwargs)
        except DatabaseError:
            raise
        except Exception:
            logger.exception(
                "Unhandled exception while processing an SES/SNS event; "
                "acking with 200 so SNS does not redeliver it."
            )
            return HttpResponse()
