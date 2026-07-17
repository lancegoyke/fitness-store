"""Coach-facing adherence — how much of the prescribed work an athlete logged.

Read-only aggregation over the athlete's current week and their own *done*
``SessionLog`` rows. It lights up the roster's long-standing ``compliance`` meter
and ``activity`` feed placeholders (``presenters`` flagged both as "Phase 2/3
concepts" awaiting logged data).

Since 2d (parity plan §3.3) delivery no longer gates what the athlete sees, so
adherence anchors on the athlete's **current** week — the ``is_current`` pointer
their own logging advances — not on the latest delivered week.

Nothing here mutates state; it's a pure read layer the presenter formats.
"""

from django.db.models import F

from .models import CoachAthlete
from .models import Plan
from .models import SessionLog
from .models import Week


def link_current_week(link):
    """The week the athlete is on, across *all* of this link's plans.

    The meter's anchor: the newest (``-modified``) plan wins — the same
    ordering the athlete home lists cards in — then its flagged ``is_current``
    week, then its earliest live week. **Archived** plans are excluded
    (matching ``working_plan`` / ``athlete_home``): an archived plan's weeks
    are ones the athlete can no longer see or log, so they must not drive the
    meter. Returns ``None`` when the link has no live weeks at all.
    """
    return (
        Week.objects.filter(
            mesocycle__plan__relationship=link,
            deleted_at__isnull=True,
        )
        .exclude(mesocycle__plan__status=Plan.Status.ARCHIVED)
        .select_related("mesocycle")
        .order_by(
            "-mesocycle__plan__modified",
            "-is_current",
            "mesocycle__order",
            "index",
        )
        .first()
    )


def _done_session_count(session_ids, athlete):
    """How many of ``session_ids`` the athlete has a *done* log for (one query)."""
    return (
        SessionLog.objects.filter(
            session_id__in=session_ids,
            athlete=athlete,
            status=SessionLog.Status.DONE,
        )
        .values("session_id")
        .distinct()
        .count()
    )


def link_compliance(link):
    """Percent (0–100) of the current week's sessions the athlete logged.

    Measured against the athlete's current week (``link_current_week``): the
    fraction of its sessions the athlete has marked *done*, rounded to a whole
    percent. Returns ``None`` — so the roster honestly hides the meter rather
    than showing a misleading ``0%`` — when there's nothing to measure: no
    plan, no live week, or a week with no sessions. ``0`` (the program is
    there, the athlete hasn't logged yet) is a real, distinct signal and is
    *not* collapsed to ``None``.

    One ``link_current_week`` query plus two small aggregates; bounded
    and proportionate to the handful of athletes a roster renders.
    """
    if link is None:
        return None
    week = link_current_week(link)
    if week is None:
        return None
    session_ids = list(week.sessions.values_list("pk", flat=True))
    if not session_ids:
        return None
    done = _done_session_count(session_ids, link.athlete)
    return round(done / len(session_ids) * 100)


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
