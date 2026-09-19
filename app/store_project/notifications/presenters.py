"""Adapt the SES event ledger into the staff deliverability dashboard's context.

Issue #507 part 2. ``models.py`` and ``ses_events.py`` (part 1) write two
tables: ``SentEmail`` (one row per message SES actually accepted) and
``EmailEvent`` (one row per SES event — send/delivery/open/click/bounce/
complaint — matched back to the ``SentEmail`` it belongs to when possible).
``email_dashboard`` is the pure, ORM-aggregated read side ``views.
EmailDashboardView`` renders — no row is loaded into Python except the capped
lists (``recent``, ``problems``, ``blacklist``, and the optional
``recipient`` lookup).

A note on "sent" counts: SES's own **Send** event only arrives once the
"Tracking" configuration set publishes it back through SNS, so it can lag or
(for a message sent before the config set was wired up) never arrive at all.
``SentEmail`` is written locally the moment ``SESBackend`` hands the message
to SES (``ses_events.record_sent_email``), so it is always available and is
the number this dashboard treats as the trustworthy "sent" count — the raw
``EmailEvent`` ``send`` count is still surfaced in ``totals`` for comparison,
but every other total (``by_kind``, ``by_day``) uses ``SentEmail`` for "sent".
"""

import datetime

from django.db.models import Count
from django.db.models import Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django_ses.models import BlacklistedEmail

from .models import TEXT_ONLY_KINDS
from .models import EmailEvent
from .models import EmailKind
from .models import SentEmail

# EmailEvent.EventType values that roll up into a per-kind / per-day bucket,
# keyed by the bucket key they fill (everything except SEND, which totals
# alone still reports — "sent" comes from SentEmail instead, see module
# docstring).
_BUCKET_EVENT_TYPES = {
    EmailEvent.EventType.DELIVERY: "delivery",
    EmailEvent.EventType.OPEN: "open",
    EmailEvent.EventType.CLICK: "click",
    EmailEvent.EventType.BOUNCE: "bounce",
    EmailEvent.EventType.COMPLAINT: "complaint",
}


def email_dashboard(*, since, recipient_query=""):
    """Aggregate the SES event ledger into the dashboard's template context.

    ``since`` bounds the window from below and the start of tomorrow (local
    time) bounds it from above — ``since <= occurred_at / sent_at < until`` —
    for ``totals``, ``by_kind``, ``by_day``, and ``problems``. The upper
    bound keeps a future-dated event (clock skew) from inflating those totals
    while ``by_day`` (which only buckets ``since``..today) has no bucket for
    it. ``recent`` and the optional ``recipient`` lookup intentionally ignore
    the window — a recipient search or the tail of raw events is a "show me
    everything about this" tool, not a windowed report.

    Contract (the view + template read these exact keys):

    - ``totals`` — ``{event_type: count}`` for every ``EmailEvent.EventType``
      value (0-filled), plus ``"sent"`` (the ``SentEmail`` count — see module
      docstring on why that, not the ``send`` event count, is authoritative).
    - ``by_kind`` — one row per ``EmailKind`` value, in choices order
      (0-filled): ``{"kind", "label", "sent", "delivery", "open", "click",
      "bounce", "complaint", "open_rate", "bounce_rate"}``. Both rates are
      percentages of ``sent``, rounded and capped at 100 (an open can be
      counted more than once by a mail-client proxy). ``open_rate`` is
      ``None`` — not ``0`` — for a kind in ``TEXT_ONLY_KINDS``: those are
      sent without an HTML part, so SES has no tracking pixel to report an
      open against and the rate would otherwise misleadingly read as "0%
      opened" rather than "not measured". ``bounce_rate`` is unaffected (a
      bounce is independent of the message having an HTML part) and, like
      ``open_rate`` for every other kind, is 0 when ``sent`` is 0.
    - ``by_day`` — one row per calendar day from ``since`` to today
      (inclusive, ascending, 0-filled): ``{"date", "sent", "delivery",
      "open", "click", "bounce", "complaint"}``.
    - ``recent`` — the 50 most recent ``EmailEvent`` rows (``select_related
      sent_email``), newest first.
    - ``problems`` — bounce + complaint events in the window, newest first,
      capped at 100.
    - ``blacklist`` — every ``django_ses.models.BlacklistedEmail`` row,
      ordered by email.
    - ``recipient`` — ``None`` when ``recipient_query`` is blank, otherwise
      ``{"query", "sent": [...], "events": [...]}``: ``events`` is every
      ``EmailEvent`` whose ``recipient`` matches case-insensitively.
      ``sent`` is every ``SentEmail`` whose own ``recipient`` matches, plus
      any ``SentEmail`` reached only through a linked ``EmailEvent``'s
      matching ``recipient`` — a multi-recipient send (e.g.
      ``send_margin_alert_email`` to every ``settings.ADMINS`` address)
      records only ``message.to[0]`` on the ``SentEmail`` row, so a later
      recipient's own events are the only place their address appears. Both
      lists are newest first, capped at 100 each.
    """
    until = timezone.make_aware(
        datetime.datetime.combine(
            timezone.localdate() + datetime.timedelta(days=1), datetime.time.min
        )
    )
    events_in_window = EmailEvent.objects.filter(
        occurred_at__gte=since, occurred_at__lt=until
    )
    sent_in_window = SentEmail.objects.filter(sent_at__gte=since, sent_at__lt=until)

    return {
        "totals": _totals(events_in_window, sent_in_window),
        "by_kind": _by_kind(events_in_window, sent_in_window),
        "by_day": _by_day(events_in_window, sent_in_window, since),
        "recent": list(
            EmailEvent.objects.select_related("sent_email").order_by("-occurred_at")[
                :50
            ]
        ),
        "problems": list(
            events_in_window.filter(
                event_type__in=(
                    EmailEvent.EventType.BOUNCE,
                    EmailEvent.EventType.COMPLAINT,
                )
            ).order_by("-occurred_at")[:100]
        ),
        "blacklist": list(BlacklistedEmail.objects.order_by("email")),
        "recipient": _recipient_lookup(recipient_query) if recipient_query else None,
    }


def _rate(numerator, denominator):
    """``numerator`` as a percentage of ``denominator``, 0 when empty, capped at 100."""
    if not denominator:
        return 0
    return min(100, round(100 * numerator / denominator))


def _totals(events_qs, sent_qs):
    totals = {value: 0 for value, _ in EmailEvent.EventType.choices}
    for row in events_qs.values("event_type").annotate(n=Count("id")):
        totals[row["event_type"]] = row["n"]
    totals["sent"] = sent_qs.count()
    return totals


def _by_kind(events_qs, sent_qs):
    sent_counts = {
        row["kind"]: row["n"] for row in sent_qs.values("kind").annotate(n=Count("id"))
    }
    event_counts = {}
    for row in events_qs.values("kind", "event_type").annotate(n=Count("id")):
        bucket_key = _BUCKET_EVENT_TYPES.get(row["event_type"])
        if bucket_key is None:
            continue
        event_counts.setdefault(row["kind"], {})[bucket_key] = row["n"]

    rows = []
    for value, label in EmailKind.choices:
        sent = sent_counts.get(value, 0)
        kind_counts = event_counts.get(value, {})
        open_count = kind_counts.get("open", 0)
        bounce_count = kind_counts.get("bounce", 0)
        rows.append(
            {
                "kind": value,
                "label": label,
                "sent": sent,
                "delivery": kind_counts.get("delivery", 0),
                "open": open_count,
                "click": kind_counts.get("click", 0),
                "bounce": bounce_count,
                "complaint": kind_counts.get("complaint", 0),
                "open_rate": None
                if value in TEXT_ONLY_KINDS
                else _rate(open_count, sent),
                "bounce_rate": _rate(bounce_count, sent),
            }
        )
    return rows


def _date_range(since):
    """Every calendar day from ``since`` to today, inclusive, ascending."""
    start = timezone.localtime(since).date()
    end = timezone.localdate()
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += datetime.timedelta(days=1)
    return days


def _by_day(events_qs, sent_qs, since):
    days = _date_range(since)
    buckets = {
        day: {
            "date": day,
            "sent": 0,
            "delivery": 0,
            "open": 0,
            "click": 0,
            "bounce": 0,
            "complaint": 0,
        }
        for day in days
    }

    for row in (
        sent_qs.annotate(day=TruncDate("sent_at")).values("day").annotate(n=Count("id"))
    ):
        bucket = buckets.get(row["day"])
        if bucket is not None:
            bucket["sent"] = row["n"]

    for row in (
        events_qs.annotate(day=TruncDate("occurred_at"))
        .values("day", "event_type")
        .annotate(n=Count("id"))
    ):
        bucket_key = _BUCKET_EVENT_TYPES.get(row["event_type"])
        if bucket_key is None:
            continue
        bucket = buckets.get(row["day"])
        if bucket is not None:
            bucket[bucket_key] = row["n"]

    return [buckets[day] for day in days]


def _recipient_lookup(query):
    """See ``email_dashboard``'s docstring for the ``recipient`` contract.

    ``sent`` matches on the ``SentEmail`` row's own ``recipient`` *or* on
    any linked ``EmailEvent``'s ``recipient`` — ``record_sent_email`` only
    ever stores ``message.to[0]``, so a multi-recipient send's second (and
    later) recipients would otherwise never turn up a ``SentEmail`` here even
    though their own events do.
    """
    return {
        "query": query,
        "sent": list(
            SentEmail.objects.filter(
                Q(recipient__iexact=query) | Q(events__recipient__iexact=query)
            )
            .distinct()
            .order_by("-sent_at")[:100]
        ),
        "events": list(
            EmailEvent.objects.select_related("sent_email")
            .filter(recipient__iexact=query)
            .order_by("-occurred_at")[:100]
        ),
    }
