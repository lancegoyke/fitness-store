"""Issue #514: ``TEXT_ONLY_KINDS`` must not drift from what senders actually send.

``presenters._by_kind`` reports ``open_rate=None`` (rendered "—") for every
``EmailKind`` in ``models.TEXT_ONLY_KINDS`` on the theory that SES can never
report an open for a message with no ``text/html`` part to carry its tracking
pixel. That theory is only true if the set is kept in sync with reality by
hand -- nothing enforces it structurally.

This module is that enforcement: it sends one real message through every
tagged sender in the app (every ``send_*`` in ``notifications.emails``,
``payments.utils.order_confirmation_email``, and allauth mail via
``store_project.users.adapters.AccountAdapter``) using Django's locmem email
backend (the default under test), and for each outgoing message asserts:

    kind in TEXT_ONLY_KINDS  <=>  the message has no text/html part

A sender that starts attaching an HTML alternative (or stops) without a
matching update to ``TEXT_ONLY_KINDS`` fails here. The closing assertion --
every ``EmailKind`` except ``OTHER`` must be covered by at least one sender
above -- means a new kind added to the enum with no sender check added here
also fails, loudly, rather than silently shipping an unverified assumption.
"""

import pytest
from django.core import mail
from django.urls import reverse

from store_project.notifications.emails import kind_from_headers
from store_project.notifications.emails import send_block_delivered_email
from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_invite_reminder_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.emails import send_contact_emails
from store_project.notifications.emails import send_margin_alert_email
from store_project.notifications.models import TEXT_ONLY_KINDS
from store_project.notifications.models import EmailKind
from store_project.payments.utils import order_confirmation_email
from store_project.users.adapters import AccountAdapter
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _has_html_part(message) -> bool:
    """Whether ``message`` actually carries a ``text/html`` part.

    Covers both shapes a sender can use: an ``EmailMultiAlternatives`` with
    ``attach_alternative(..., "text/html")`` (``message.alternatives``), and
    the (unused-in-this-app-but-allauth-supports-it) case of an
    ``EmailMessage`` whose *main* body is HTML (``content_subtype``).
    """
    if message.content_subtype == "html":
        return True
    return any(
        mimetype == "text/html"
        for _content, mimetype in getattr(message, "alternatives", [])
    )


class TestTextOnlyKindsMatchRealSenders:
    """Drive every real tagged sender and check its HTML-ness against the set."""

    def _check(self, message) -> EmailKind:
        """Assert ``kind in TEXT_ONLY_KINDS`` iff ``message`` has no HTML part.

        Returns the recovered ``kind`` so callers can accumulate coverage.
        """
        kind = kind_from_headers(message.extra_headers)
        assert kind != EmailKind.OTHER, (
            f"{message.subject!r} was not tagged with a real EmailKind "
            "(kind_from_headers fell back to OTHER)"
        )
        has_html = _has_html_part(message)
        if kind in TEXT_ONLY_KINDS:
            assert not has_html, (
                f"{kind} is in TEXT_ONLY_KINDS but was sent with an HTML "
                "alternative -- SES *can* track opens for it now, remove it "
                "from TEXT_ONLY_KINDS"
            )
        else:
            assert has_html, (
                f"{kind} is not in TEXT_ONLY_KINDS but was sent with no HTML "
                "part -- SES can never report an open for it, add it to "
                "TEXT_ONLY_KINDS"
            )
        return kind

    def test_every_real_sender_matches_text_only_kinds(self, client, settings):
        kinds_seen = set()

        # notifications.emails -----------------------------------------
        send_contact_emails("Question", "Hello there", "visitor@example.com")
        kinds_seen |= {self._check(m) for m in mail.outbox}
        mail.outbox.clear()

        coach = UserFactory()
        send_coach_invite_email(
            coach=coach, email="athlete@example.com", accept_url="https://x.test/claim/"
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        send_coach_invite_reminder_email(
            coach=coach, email="athlete@example.com", accept_url="https://x.test/claim/"
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        athlete = UserFactory()
        coach2 = UserFactory(email="coach2@example.com")
        send_coach_request_email(
            athlete=athlete, coach=coach2, roster_url="https://x.test/meso/"
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        settings.ADMINS = [("Owner", "owner@example.com")]
        alert = type(
            "Alert",
            (),
            {
                "label": "Coach X",
                "billing_status": "active",
                "billable_seats": 1,
                "totals": type("Totals", (), {"runs": 1, "cost": 1})(),
                "revenue": 2,
                "margin": 1,
                "cost_to_revenue_ratio": 0.5,
            },
        )()
        send_margin_alert_email(alerts=[alert], month_label="2026-06", threshold=0.5)
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        plan = type("Plan", (), {"title": "Hypertrophy Block"})()
        send_block_delivered_email(
            athlete=athlete,
            coach=coach2,
            plan=plan,
            week_count=2,
            home_url="https://x.test/meso/me/",
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        # payments.utils --------------------------------------------------
        buyer = UserFactory(email="buyer@example.com")
        product = type("Product", (), {"name": "Test Program"})()
        order_confirmation_email({"amount_total": 2500}, product, buyer)
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        # allauth mail via store_project.users.adapters.AccountAdapter ---
        # Distinct addresses from users/tests/test_adapters.py's own fixtures:
        # allauth's default "confirm_email"/"reset_password" rate limits are
        # keyed by email and backed by the (process-wide, not per-test) cache,
        # so reusing the same address across test modules in one test run can
        # silently swallow the second send.
        client.post(
            reverse("account_signup"),
            {
                "email": "text-only-kinds-signup@example.com",
                "password1": "a very unguessable pw 42",
            },
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        UserFactory(email="text-only-kinds-reset@example.com")
        client.post(
            reverse("account_reset_password"),
            {"email": "text-only-kinds-reset@example.com"},
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        # An allauth template_prefix not in adapters._KIND_BY_TEMPLATE_NAME
        # (e.g. the "password changed" notice) falls back to ACCOUNT_NOTICE
        # -- allauth ships this one as text-only, matching TEXT_ONLY_KINDS.
        AccountAdapter().send_mail(
            "account/email/password_changed", "notice@example.com", {}
        )
        kinds_seen.add(self._check(mail.outbox[0]))
        mail.outbox.clear()

        assert kinds_seen == {kind for kind in EmailKind if kind != EmailKind.OTHER}
