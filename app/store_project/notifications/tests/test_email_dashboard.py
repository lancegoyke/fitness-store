"""Issue #507 part 2 — the staff email deliverability dashboard view.

Mirrors ``meso.views.TourFunnelView`` / ``UsageDashboardView``'s staff gate
exactly (anon → login, non-staff → 403, staff → 200) and wires the
``?days=7|30|90`` window (default 30; invalid input falls back to 30 with a
flashed ``messages.error``, mirroring ``UsageDashboardView._window``) and the
``?q=<email>`` recipient lookup on top of ``presenters.email_dashboard``.

Issue #514 moved the template off the Meso shell onto ``admin/base_site.html``
(``TestAdminChrome``) and dropped ``ctx["active"] = "email"`` -- nothing reads
it now that the page no longer renders the Meso nav.
"""

import datetime
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _url(**params):
    # Called inside each test (not at module scope) so a missing route fails
    # the individual test rather than erroring collection of the whole module.
    return reverse("notifications:email_dashboard")


def _sent(*, kind=EmailKind.OTHER, recipient="athlete@example.com", subject="Hi"):
    return SentEmail.objects.create(
        ses_message_id=f"ses-{SentEmail.objects.count()}-{recipient}",
        kind=kind,
        recipient=recipient,
        subject=subject,
    )


def _event(event_type, *, kind=EmailKind.OTHER, recipient="athlete@example.com"):
    return EmailEvent.objects.create(
        event_type=event_type,
        ses_message_id="ses-shared",
        sns_message_id=f"sns-{EmailEvent.objects.count()}",
        recipient=recipient,
        kind=kind,
        occurred_at=timezone.now(),
    )


class TestUrl:
    def test_reverses_to_the_backside_mount(self):
        assert reverse("notifications:email_dashboard") == "/backside/email/"


class TestAdminChrome:
    """Issue #514: the dashboard moved off the Meso shell onto ``admin/base_site.html``.

    It must render with the real Django admin header/breadcrumbs/nav sidebar
    (``admin.site.each_context`` in the view's context) and carry no trace of
    Meso.
    """

    def test_renders_on_the_admin_base(self, client):
        client.force_login(UserFactory(is_staff=True))

        body = client.get(_url()).content.decode()

        assert 'id="site-name"' in body
        assert 'class="breadcrumbs"' in body
        assert "Email deliverability" in body

    def test_no_meso_chrome(self, client):
        client.force_login(UserFactory(is_staff=True))

        body = client.get(_url()).content.decode()

        assert "Meso" not in body
        assert "meso-" not in body

    def test_breadcrumbs_home_links_to_admin_index(self, client):
        client.force_login(UserFactory(is_staff=True))

        body = client.get(_url()).content.decode()

        assert reverse("admin:index") in body


class TestGate:
    def test_anonymous_is_redirected_to_login(self, client):
        resp = client.get(_url())
        assert resp.status_code == 302
        assert "/accounts/login/" in resp["Location"]

    def test_authenticated_non_staff_is_forbidden(self, client):
        client.force_login(UserFactory())
        resp = client.get(_url())
        assert resp.status_code == 403

    def test_staff_gets_the_dashboard(self, client):
        client.force_login(UserFactory(is_staff=True))
        resp = client.get(_url())
        assert resp.status_code == 200
        assert resp.templates[0].name == "notifications/email_dashboard.html"


def _table(body, testid):
    """The HTML of the ``<table data-testid="{testid}">...</table>`` element."""
    match = re.search(rf'data-testid="{testid}".*?</table>', body, re.DOTALL)
    assert match, f"no table with data-testid={testid!r} found"
    return match.group(0)


class TestTextOnlyOpenRateRendersAsDash:
    """A ``None`` open_rate (text-only kinds -- #514) renders "—", not "0%"."""

    def test_password_reset_row_shows_a_dash_not_a_percentage(self, client):
        _sent(kind=EmailKind.PASSWORD_RESET)
        client.force_login(UserFactory(is_staff=True))

        table = _table(client.get(_url()).content.decode(), "email-by-kind")

        assert "None" not in table
        assert "—" in table

    def test_html_kind_still_shows_a_percentage(self, client):
        _sent(kind=EmailKind.ORDER_CONFIRMATION)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.ORDER_CONFIRMATION)
        client.force_login(UserFactory(is_staff=True))

        table = _table(client.get(_url()).content.decode(), "email-by-kind")

        assert "100%" in table


class TestContext:
    def test_presenter_keys_are_present(self, client):
        client.force_login(UserFactory(is_staff=True))

        ctx = client.get(_url()).context

        for key in (
            "totals",
            "by_kind",
            "by_day",
            "recent",
            "problems",
            "blacklist",
            "recipient",
        ):
            assert key in ctx
        assert ctx["days"] == 30
        assert ctx["q"] == ""
        assert ctx["recipient"] is None


class TestDaysWindow:
    def test_defaults_to_30(self, client):
        client.force_login(UserFactory(is_staff=True))
        ctx = client.get(_url()).context
        assert ctx["days"] == 30

    @pytest.mark.parametrize("days", [7, 30, 90])
    def test_accepts_valid_windows(self, client, days):
        client.force_login(UserFactory(is_staff=True))
        ctx = client.get(_url(), {"days": days}).context
        assert ctx["days"] == days

    def test_invalid_days_falls_back_to_30_with_a_message(self, client):
        client.force_login(UserFactory(is_staff=True))
        resp = client.get(_url(), {"days": "abc"})
        assert resp.status_code == 200
        assert resp.context["days"] == 30
        messages = [str(m) for m in resp.context["messages"]]
        assert any("abc" in m for m in messages)

    def test_out_of_range_days_falls_back_to_30_with_a_message(self, client):
        client.force_login(UserFactory(is_staff=True))
        resp = client.get(_url(), {"days": "14"})
        assert resp.status_code == 200
        assert resp.context["days"] == 30
        messages = [str(m) for m in resp.context["messages"]]
        assert any("14" in m for m in messages)

    def test_only_within_window_events_are_counted(self, client):
        old = timezone.now() - datetime.timedelta(days=60)
        SentEmail.objects.create(
            ses_message_id="ses-old", recipient="old@example.com", sent_at=old
        )
        _sent(recipient="recent@example.com")
        client.force_login(UserFactory(is_staff=True))

        ctx7 = client.get(_url(), {"days": 7}).context
        ctx90 = client.get(_url(), {"days": 90}).context

        assert ctx7["totals"]["sent"] == 1
        assert ctx90["totals"]["sent"] == 2


class TestRecipientLookup:
    def test_case_insensitive_lookup_scopes_to_that_recipient(self, client):
        _sent(recipient="Match@Example.com", subject="Matched subject")
        _sent(recipient="other@example.com", subject="Other subject")
        client.force_login(UserFactory(is_staff=True))

        resp = client.get(_url(), {"q": "match@example.com"})

        assert resp.status_code == 200
        assert resp.context["q"] == "match@example.com"
        recipient_ctx = resp.context["recipient"]
        assert recipient_ctx["sent"][0].recipient.lower() == "match@example.com"
        assert len(recipient_ctx["sent"]) == 1
        body = resp.content.decode()
        assert "Matched subject" in body
        assert "Other subject" not in body

    def test_blank_q_does_not_run_a_lookup(self, client):
        client.force_login(UserFactory(is_staff=True))
        resp = client.get(_url())
        assert resp.context["recipient"] is None
