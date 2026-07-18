"""Coach-facing adherence — cadence, not a per-week prescription percent.

Read-only aggregation over an athlete's own *done* ``SessionLog`` rows. It
lights up the roster's long-standing recency signal and ``activity`` feed
placeholders (``presenters`` flagged both as "Phase 2/3 concepts" awaiting
logged data).

Programs are date-less, so there is no "current week" to measure a percent
against (docs/meso/remove-current-week-plan.md §4a, decided 2026-07-18):
"cadence is more important to know because it can alert how much effort
they're putting in" than progress through a fixed denominator. The signal is
**recency** — how long since the athlete's last logged (done) session — plus a
secondary rolling volume count, both keyed off ``SessionLog.created_at`` (the
server-stamped log-write clock, always present and monotonic, unlike the
nullable athlete-entered ``date``). Two caveats apply to both: ``created_at``
is *write* time, so re-saving an old workout reads as fresh; and any
recency-based signal penalizes an athlete who trains but doesn't log — a
coaching-culture tradeoff, not a code one.

Nothing here mutates state; it's a pure read layer the presenter formats.
"""

from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import CoachAthlete
from .models import Plan
from .models import SessionLog


def link_last_trained(link):
    """This link's athlete's most recent *done* ``SessionLog``, any live plan.

    The cadence signal's raw material: newest ``created_at`` wins, scanning
    every one of the link's **non-archived** plans (not just the newest one —
    a stale log on an older still-live plan is just as real a "last trained"
    as one on the newest). Archived plans are excluded, matching
    ``recent_logs`` / the old current-week anchor: their history is no longer
    something the athlete can see or add to, so it must not read as current
    cadence. Returns ``None`` when the link has no done logs at all (``link``
    itself may also be ``None`` — a convenience for callers that fetch it
    once and pass it straight through).
    """
    if link is None:
        return None
    return (
        SessionLog.objects.filter(
            session__week__mesocycle__plan__relationship=link,
            athlete=link.athlete,
            status=SessionLog.Status.DONE,
        )
        .exclude(session__week__mesocycle__plan__status=Plan.Status.ARCHIVED)
        .select_related("session__week__mesocycle__plan")
        .order_by("-created_at")
        .first()
    )


def link_recency_days(link):
    """Whole days since ``link_last_trained`` — the roster pill's tone input.

    ``0`` for a log written today; ``None`` when there's no done log yet
    (mirrors the old meter's hidden state — the roster shows "No sessions
    yet" rather than a misleading number).
    """
    log = link_last_trained(link)
    if log is None:
        return None
    return (timezone.now() - log.created_at).days


def link_session_count(link, *, days=14):
    """Distinct DONE sessions this link's athlete logged in the last ``days``.

    The cadence signal's volume half (decision Option B): a plain count, not a
    percent — a date-less program exposes no prescribed weekly frequency to
    divide by, so there is no honest denominator to build a meter from. Same
    archived-plan exclusion as ``link_last_trained``. A session logged done
    more than once (the model allows dated history) counts once.
    """
    if link is None:
        return 0
    since = timezone.now() - timedelta(days=days)
    return (
        SessionLog.objects.filter(
            session__week__mesocycle__plan__relationship=link,
            athlete=link.athlete,
            status=SessionLog.Status.DONE,
            created_at__gte=since,
        )
        .exclude(session__week__mesocycle__plan__status=Plan.Status.ARCHIVED)
        .values("session_id")
        .distinct()
        .count()
    )


def recent_logs(coach, *, limit=8):
    """The coach's athletes' most recently completed sessions (newest first).

    Scoped to the coach's *active* links (an ended relationship's history drops
    off the feed, matching the roster's athlete list), to non-**archived** plans
    (an archived plan's sessions must not resurface), and to *done* logs only —
    the feed answers "who finished a session," not "who opened
    one." Ordered by when the log was
    written (``created_at``), so re-saving an old workout surfaces as fresh
    activity and the order never depends on the nullable workout ``date``. The
    log's ``athlete`` is tied to the plan's own athlete: the write path always
    enforces this, but the model carries no DB constraint, so a stray mismatched
    row (admin / import) must not surface an unrelated name + a profile link the
    coach can't open. ``select_related`` the athlete + session so the presenter
    formats each event without a per-row query.
    """
    return list(
        SessionLog.objects.filter(
            status=SessionLog.Status.DONE,
            session__week__mesocycle__plan__relationship__coach=coach,
            session__week__mesocycle__plan__relationship__status=(
                CoachAthlete.Status.ACTIVE
            ),
            athlete=F("session__week__mesocycle__plan__relationship__athlete"),
        )
        .exclude(session__week__mesocycle__plan__status=Plan.Status.ARCHIVED)
        .select_related("athlete", "session")
        .order_by("-created_at")[:limit]
    )
