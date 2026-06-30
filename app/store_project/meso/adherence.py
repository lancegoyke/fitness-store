"""Coach-facing adherence — how much of the delivered work an athlete logged.

Read-only aggregation over delivered weeks and the athletes' own *done*
``SessionLog`` rows. It lights up the roster's long-standing ``compliance`` meter
and ``activity`` feed placeholders (``presenters`` flagged both as "Phase 2/3
concepts" awaiting logged data) — delivery + logging have existed since the
athlete slice, so the data is finally there to measure.

Nothing here mutates state; it's a pure read layer the presenter formats.
"""

from django.db.models import F

from .models import CoachAthlete
from .models import Plan
from .models import SessionLog
from .models import Week


def link_latest_delivered_week(link):
    """The most recently delivered week across *all* of this link's plans.

    Spans both the coach's individual plan and any group-delivery snapshot
    (``source_group`` set) rooted at this relationship — both are work this coach
    delivered to the athlete, so adherence should reflect whichever week was
    delivered most recently. **Archived** plans are excluded (matching
    ``working_plan`` / ``athlete_home``): removing an athlete from a group
    archives their materialized snapshot while the link stays active, and the
    athlete can no longer see or log it, so it must not drive the meter. Returns
    ``None`` when the coach hasn't delivered anything live to this athlete yet.
    """
    return (
        Week.objects.filter(
            mesocycle__plan__relationship=link,
            delivered_at__isnull=False,
        )
        .exclude(mesocycle__plan__status=Plan.Status.ARCHIVED)
        .select_related("mesocycle")
        .order_by("-delivered_at")
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
    """Percent (0–100) of the latest delivered week's sessions the athlete logged.

    Measured against the most recently delivered week
    (``link_latest_delivered_week``): the fraction of its sessions the athlete
    has marked *done*, rounded to a whole percent. Returns ``None`` — so the
    roster honestly hides the meter rather than showing a misleading ``0%`` —
    when there's nothing to measure: no plan, no delivered week, or a delivered
    week with no sessions. ``0`` (the coach delivered, the athlete hasn't logged
    yet) is a real, distinct signal and is *not* collapsed to ``None``.

    One ``link_latest_delivered_week`` query plus two small aggregates; bounded
    and proportionate to the handful of athletes a roster renders.
    """
    if link is None:
        return None
    week = link_latest_delivered_week(link)
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
    (a removed group member's archived snapshot must not resurface), and to
    *done* logs only — the feed answers "who finished a session," not "who opened
    one." Spans individual and group-delivered sessions alike, since both root at
    the athlete's relationship with the coach. Ordered by when the log was
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
