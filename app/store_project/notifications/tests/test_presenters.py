"""Issue #507 part 2 — the deliverability-dashboard presenter.

``presenters.email_dashboard`` is the pure aggregation the staff dashboard
view (``EmailDashboardView``) renders: window totals, a per-kind breakdown, a
per-day trend, the most recent events, the problem (bounce/complaint) events,
the current SES blacklist, and an optional recipient lookup. Everything is
ORM-aggregated (``values(...).annotate(Count(...))``); these tests pin the
exact dict/list contract the view + template depend on.

Pre-implementation this is RED: ``store_project.notifications.presenters``
doesn't exist yet, so every test fails at import/collection with
``ModuleNotFoundError``.
"""

import datetime

import pytest
from django.utils import timezone
from django_ses.models import BlacklistedEmail

from store_project.notifications import presenters
from store_project.notifications.models import TEXT_ONLY_KINDS
from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail

pytestmark = pytest.mark.django_db


def _sent(*, kind=EmailKind.OTHER, recipient="athlete@example.com", sent_at=None):
    return SentEmail.objects.create(
        ses_message_id=f"ses-{SentEmail.objects.count()}-{recipient}",
        kind=kind,
        recipient=recipient,
        sent_at=sent_at or timezone.now(),
    )


def _event(
    event_type,
    *,
    kind=EmailKind.OTHER,
    recipient="athlete@example.com",
    occurred_at=None,
    **extra,
):
    return EmailEvent.objects.create(
        event_type=event_type,
        ses_message_id="ses-shared",
        sns_message_id=f"sns-{EmailEvent.objects.count()}",
        recipient=recipient,
        kind=kind,
        occurred_at=occurred_at or timezone.now(),
        **extra,
    )


# ---------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------


class TestTotals:
    def test_zero_filled_for_every_event_type_plus_sent(self):
        since = timezone.now() - datetime.timedelta(days=1)

        result = presenters.email_dashboard(since=since)

        assert result["totals"] == {
            "send": 0,
            "delivery": 0,
            "open": 0,
            "click": 0,
            "bounce": 0,
            "complaint": 0,
            "sent": 0,
        }

    def test_counts_within_window_only(self):
        since = timezone.now() - datetime.timedelta(days=7)
        outside = since - datetime.timedelta(days=1)
        _sent(sent_at=timezone.now())
        _sent(sent_at=outside)  # excluded
        _event(EmailEvent.EventType.DELIVERY, occurred_at=timezone.now())
        _event(EmailEvent.EventType.DELIVERY, occurred_at=outside)  # excluded
        _event(EmailEvent.EventType.OPEN, occurred_at=timezone.now())
        _event(EmailEvent.EventType.BOUNCE, occurred_at=timezone.now())

        result = presenters.email_dashboard(since=since)

        assert result["totals"]["sent"] == 1
        assert result["totals"]["delivery"] == 1
        assert result["totals"]["open"] == 1
        assert result["totals"]["bounce"] == 1
        assert result["totals"]["send"] == 0
        assert result["totals"]["click"] == 0
        assert result["totals"]["complaint"] == 0


# ---------------------------------------------------------------------------
# by_kind
# ---------------------------------------------------------------------------


class TestByKind:
    def test_zero_filled_in_choices_order(self):
        since = timezone.now() - datetime.timedelta(days=1)

        result = presenters.email_dashboard(since=since)

        assert [row["kind"] for row in result["by_kind"]] == [
            value for value, _ in EmailKind.choices
        ]
        for row in result["by_kind"]:
            assert row["sent"] == 0
            assert row["bounce_rate"] == 0
            if row["kind"] in TEXT_ONLY_KINDS:
                assert row["open_rate"] is None
            else:
                assert row["open_rate"] == 0

    def test_counts_and_rates_per_kind(self):
        since = timezone.now() - datetime.timedelta(days=1)
        _sent(kind=EmailKind.COACH_INVITE)
        _sent(kind=EmailKind.COACH_INVITE)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.COACH_INVITE)
        _event(EmailEvent.EventType.BOUNCE, kind=EmailKind.COACH_INVITE)

        result = presenters.email_dashboard(since=since)

        row = next(r for r in result["by_kind"] if r["kind"] == EmailKind.COACH_INVITE)
        assert row["label"] == EmailKind.COACH_INVITE.label
        assert row["sent"] == 2
        assert row["open"] == 1
        assert row["bounce"] == 1
        assert row["open_rate"] == 50
        assert row["bounce_rate"] == 50

    def test_open_rate_is_capped_at_100(self):
        since = timezone.now() - datetime.timedelta(days=1)
        _sent(kind=EmailKind.OTHER)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.OTHER)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.OTHER)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.OTHER)

        result = presenters.email_dashboard(since=since)

        row = next(r for r in result["by_kind"] if r["kind"] == EmailKind.OTHER)
        assert row["open"] == 3
        assert row["open_rate"] == 100

    def test_zero_sent_gives_zero_rate(self):
        since = timezone.now() - datetime.timedelta(days=1)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.CONTACT_ACK)

        result = presenters.email_dashboard(since=since)

        row = next(r for r in result["by_kind"] if r["kind"] == EmailKind.CONTACT_ACK)
        assert row["open"] == 1
        assert row["sent"] == 0
        assert row["open_rate"] == 0


# ---------------------------------------------------------------------------
# text-only kinds: open_rate is None, never a misleading 0%
# ---------------------------------------------------------------------------


class TestTextOnlyOpenRate:
    """SES can only track an open via a tracking pixel in an HTML part.

    ``account_confirmation``, ``password_reset``, ``account_notice``, and
    ``honeypot_alert`` are sent as plain text (issue #514) — their
    ``open_rate`` must be ``None`` regardless of sent/open counts, not a
    number that implies the metric is meaningful for them.
    """

    @pytest.mark.parametrize("kind", sorted(TEXT_ONLY_KINDS))
    def test_open_rate_is_none_even_with_opens_recorded(self, kind):
        since = timezone.now() - datetime.timedelta(days=1)
        _sent(kind=kind)
        _event(EmailEvent.EventType.OPEN, kind=kind)

        result = presenters.email_dashboard(since=since)

        row = next(r for r in result["by_kind"] if r["kind"] == kind)
        assert row["open"] == 1
        assert row["open_rate"] is None

    def test_html_kinds_still_get_a_numeric_rate(self):
        since = timezone.now() - datetime.timedelta(days=1)
        _sent(kind=EmailKind.ORDER_CONFIRMATION)
        _event(EmailEvent.EventType.OPEN, kind=EmailKind.ORDER_CONFIRMATION)

        result = presenters.email_dashboard(since=since)

        row = next(
            r for r in result["by_kind"] if r["kind"] == EmailKind.ORDER_CONFIRMATION
        )
        assert row["open_rate"] == 100


# ---------------------------------------------------------------------------
# by_day
# ---------------------------------------------------------------------------


class TestByDay:
    def test_zero_filled_ascending_from_since_to_today(self):
        since = timezone.now() - datetime.timedelta(days=2)

        result = presenters.email_dashboard(since=since)

        dates = [row["date"] for row in result["by_day"]]
        expected_start = timezone.localtime(since).date()
        expected_end = timezone.localdate()
        expected = []
        d = expected_start
        while d <= expected_end:
            expected.append(d)
            d += datetime.timedelta(days=1)
        assert dates == expected
        assert all(row["sent"] == 0 for row in result["by_day"])

    def test_buckets_by_calendar_day(self):
        today = timezone.localdate()
        since = timezone.now() - datetime.timedelta(days=1)
        yesterday_dt = timezone.now() - datetime.timedelta(days=1)
        _sent(sent_at=timezone.now())
        _event(EmailEvent.EventType.DELIVERY, occurred_at=timezone.now())
        _event(EmailEvent.EventType.OPEN, occurred_at=yesterday_dt)

        result = presenters.email_dashboard(since=since)

        by_date = {row["date"]: row for row in result["by_day"]}
        assert by_date[today]["sent"] == 1
        assert by_date[today]["delivery"] == 1
        yesterday = timezone.localtime(yesterday_dt).date()
        assert by_date[yesterday]["open"] == 1


# ---------------------------------------------------------------------------
# window upper bound
# ---------------------------------------------------------------------------


class TestWindowUpperBound:
    """A future-dated event (clock skew) must not inflate the window.

    ``since`` only bounded the window from below — a future ``occurred_at``/
    ``sent_at`` slipped into every other windowed queryset (``totals``,
    ``by_kind``, ``problems``) while ``by_day`` (which only buckets
    ``since``..today) silently dropped it, so totals/by_kind/problems could
    report an event that never showed up in the trend. ``until`` (start of
    tomorrow, local time) closes the window on both ends.
    """

    def test_future_dated_event_excluded_from_every_windowed_total(self):
        since = timezone.now() - datetime.timedelta(days=7)
        future = timezone.now() + datetime.timedelta(days=2)
        _sent(sent_at=future)
        _event(EmailEvent.EventType.BOUNCE, occurred_at=future)

        result = presenters.email_dashboard(since=since)

        assert result["totals"]["sent"] == 0
        assert result["totals"]["bounce"] == 0
        row = next(r for r in result["by_kind"] if r["kind"] == EmailKind.OTHER)
        assert row["sent"] == 0
        assert row["bounce"] == 0
        assert result["problems"] == []

    def test_event_dated_today_still_counts_once(self):
        since = timezone.now() - datetime.timedelta(days=7)
        _sent(sent_at=timezone.now())
        _event(EmailEvent.EventType.DELIVERY, occurred_at=timezone.now())

        result = presenters.email_dashboard(since=since)

        assert result["totals"]["sent"] == 1
        assert result["totals"]["delivery"] == 1
        today = timezone.localdate()
        by_date = {row["date"]: row for row in result["by_day"]}
        assert by_date[today]["sent"] == 1
        assert by_date[today]["delivery"] == 1


# ---------------------------------------------------------------------------
# recent
# ---------------------------------------------------------------------------


class TestRecent:
    def test_returns_newest_first_regardless_of_window(self):
        since = timezone.now()  # the window is essentially "now" only
        old = timezone.now() - datetime.timedelta(days=400)
        newer = timezone.now() - datetime.timedelta(days=1)
        e_old = _event(EmailEvent.EventType.OPEN, occurred_at=old)
        e_new = _event(EmailEvent.EventType.CLICK, occurred_at=newer)

        result = presenters.email_dashboard(since=since)

        assert result["recent"][0] == e_new
        assert e_old in result["recent"]

    def test_caps_at_50(self):
        since = timezone.now() - datetime.timedelta(days=1)
        for i in range(55):
            _event(
                EmailEvent.EventType.OPEN,
                occurred_at=timezone.now() - datetime.timedelta(minutes=i),
            )

        result = presenters.email_dashboard(since=since)

        assert len(result["recent"]) == 50


# ---------------------------------------------------------------------------
# problems
# ---------------------------------------------------------------------------


class TestProblems:
    def test_bounce_and_complaint_only_within_window_newest_first(self):
        since = timezone.now() - datetime.timedelta(days=1)
        outside = since - datetime.timedelta(days=1)
        b_out = _event(EmailEvent.EventType.BOUNCE, occurred_at=outside)
        b_in = _event(EmailEvent.EventType.BOUNCE, occurred_at=timezone.now())
        c_in = _event(
            EmailEvent.EventType.COMPLAINT,
            occurred_at=timezone.now() - datetime.timedelta(minutes=1),
        )
        _event(EmailEvent.EventType.DELIVERY, occurred_at=timezone.now())

        result = presenters.email_dashboard(since=since)

        assert b_out not in result["problems"]
        assert result["problems"] == [b_in, c_in]

    def test_capped_at_100(self):
        since = timezone.now() - datetime.timedelta(days=1)
        for i in range(105):
            _event(
                EmailEvent.EventType.BOUNCE,
                occurred_at=timezone.now() - datetime.timedelta(minutes=i),
            )

        result = presenters.email_dashboard(since=since)

        assert len(result["problems"]) == 100


# ---------------------------------------------------------------------------
# blacklist
# ---------------------------------------------------------------------------


class TestBlacklist:
    def test_all_rows_ordered_by_email(self):
        BlacklistedEmail.objects.create(email="zzz@example.com")
        BlacklistedEmail.objects.create(email="aaa@example.com")
        since = timezone.now() - datetime.timedelta(days=1)

        result = presenters.email_dashboard(since=since)

        assert [b.email for b in result["blacklist"]] == [
            "aaa@example.com",
            "zzz@example.com",
        ]


# ---------------------------------------------------------------------------
# recipient lookup
# ---------------------------------------------------------------------------


class TestRecipientLookup:
    def test_blank_query_returns_none(self):
        since = timezone.now() - datetime.timedelta(days=1)

        result = presenters.email_dashboard(since=since)

        assert result["recipient"] is None

    def test_case_insensitive_match_scoped_to_recipient(self):
        since = timezone.now() - datetime.timedelta(days=1)
        matched_sent = _sent(recipient="Match@Example.com")
        _sent(recipient="other@example.com")
        matched_event = _event(EmailEvent.EventType.OPEN, recipient="match@example.com")
        _event(EmailEvent.EventType.OPEN, recipient="other@example.com")

        result = presenters.email_dashboard(
            since=since, recipient_query="MATCH@example.com"
        )

        assert result["recipient"]["query"] == "MATCH@example.com"
        assert result["recipient"]["sent"] == [matched_sent]
        assert result["recipient"]["events"] == [matched_event]

    def test_matches_sent_via_a_linked_event_recipient(self):
        """A multi-recipient send only stores ``message.to[0]`` on ``SentEmail``.

        ``record_sent_email`` writes only the first recipient onto the
        ``SentEmail`` row it creates (e.g. every ``settings.ADMINS`` address
        for ``send_margin_alert_email``), so a later recipient's own
        ``EmailEvent`` rows (linked back via ``sent_email``) are the only
        place their address appears. The lookup must still surface the
        ``SentEmail`` for that recipient's search.
        """
        since = timezone.now() - datetime.timedelta(days=1)
        sent = _sent(recipient="first@example.com")
        _event(
            EmailEvent.EventType.DELIVERY,
            recipient="second@example.com",
            sent_email=sent,
        )

        result = presenters.email_dashboard(
            since=since, recipient_query="SECOND@example.com"
        )

        assert result["recipient"]["sent"] == [sent]

    def test_unrelated_query_matches_no_sent_rows(self):
        since = timezone.now() - datetime.timedelta(days=1)
        sent = _sent(recipient="first@example.com")
        _event(
            EmailEvent.EventType.DELIVERY,
            recipient="second@example.com",
            sent_email=sent,
        )

        result = presenters.email_dashboard(
            since=since, recipient_query="nobody@example.com"
        )

        assert result["recipient"]["sent"] == []

    def test_ignores_the_window(self):
        since = timezone.now() - datetime.timedelta(days=1)
        old = timezone.now() - datetime.timedelta(days=400)
        old_sent = _sent(recipient="long-ago@example.com", sent_at=old)
        old_event = _event(
            EmailEvent.EventType.OPEN, recipient="long-ago@example.com", occurred_at=old
        )

        result = presenters.email_dashboard(
            since=since, recipient_query="long-ago@example.com"
        )

        assert result["recipient"]["sent"] == [old_sent]
        assert result["recipient"]["events"] == [old_event]

    def test_capped_at_100_each(self):
        since = timezone.now() - datetime.timedelta(days=1)
        for i in range(105):
            when = timezone.now() - datetime.timedelta(minutes=i)
            _sent(recipient="busy@example.com", sent_at=when)
            _event(
                EmailEvent.EventType.OPEN,
                recipient="busy@example.com",
                occurred_at=when,
            )

        result = presenters.email_dashboard(
            since=since, recipient_query="busy@example.com"
        )

        assert len(result["recipient"]["sent"]) == 100
        assert len(result["recipient"]["events"]) == 100
