"""Adapt real Meso models into the dict shapes the templates expect.

The roster/profile templates were built against a fixtures module (since
retired). Phase 1 feeds them real, scoped data for everything that exists yet —
the athlete, their training history, and their (global) contraindications.
Roster **compliance** (the per-athlete adherence meter) and **activity** (the
recent-completed-sessions feed) are now wired off real logged data via
``adherence``; the profile's ``compliance`` and ``has_program`` stay neutral
until those surfaces grow their own slices.
"""

import math
from collections import defaultdict

from django.db.models import Count
from django.db.models import Exists
from django.db.models import OuterRef
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from . import adherence
from . import tour
from .billing import access as billing_access
from .billing import agent_usage_report
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachSubscription
from .models import Plan
from .models import SessionLog
from .models import TourEvent
from .models import Week
from .models import WeekDelivery
from .models import parsed_set_is_hidden
from .one_rm import key_str
from .one_rm import one_rm_values
from .parsing import cell_should_warn
from .personal_records import new_records_in
from .personal_records import personal_records
from .serializers import _fmt_num
from .serializers import _num
from .serializers import _phase_states
from .serializers import _week_label
from .serializers import current_week
from .serializers import diff_week_snapshots
from .serializers import initials
from .serializers import serialize_mesocycle
from .serializers import serialize_mesocycle_grid
from .serializers import serialize_new_record
from .serializers import serialize_prescription
from .serializers import serialize_proposed_change
from .serializers import serialize_week_snapshot

#: Paywall display copy for the flat monthly Pro plan (D14). The authoritative
#: amount is the Stripe Price the owner configures; this is the marketing string
#: shown in the roster card, the designer upgrade CTA, and the become-a-coach
#: tiers — one constant so the three surfaces never drift.
PRICE_SUMMARY = "$19/mo — unlimited athletes"


def _age(user):
    birthday = getattr(user, "birthday", None)
    if not birthday:
        return None
    today = timezone.localdate()
    return (
        today.year
        - birthday.year
        - ((today.month, today.day) < (birthday.month, birthday.day))
    )


def _training_label(user):
    profile = getattr(user, "athlete_profile", None)
    months = profile.training_months if profile else None
    if months is None:
        return None
    if months < 12:
        return f"{months} mo training"
    years, rem = divmod(months, 12)
    if rem:
        return f"{years} yr {rem} mo training"
    return f"{years} yr training"


def _active_contraindications(user):
    # ``contraindications`` is prefetched on the roster/profile querysets.
    return [c for c in user.contraindications.all() if c.active]


def _recency_label(days):
    """The "Last trained …" copy for a cadence days-count; ``None`` -> hidden state.

    Mirrors the old meter's hidden-when-nothing-to-measure behavior, but as
    honest copy rather than an absent element — a coach scanning the roster
    should see "No sessions yet" rather than a blank cell.
    """
    if days is None:
        return "No sessions yet"
    if days == 0:
        return "Last trained today"
    if days == 1:
        return "Last trained 1 day ago"
    return f"Last trained {days} days ago"


def _recency_tone(days):
    """Tone band for the roster's cadence pill (§4a, decided 2026-07-18).

    <=3d reads as keeping up (``ok``, green), 4-9d as slipping (``warn``,
    amber), >=10d as lapsed (``danger``, red) — an explicit escalation past
    plain "behind." ``None`` (no sessions logged yet) is neutral, not alarming.
    """
    if days is None:
        return "muted"
    if days <= 3:
        return "ok"
    if days <= 9:
        return "warn"
    return "danger"


def roster_athlete(
    user,
    *,
    suspended=False,
    demo=False,
    self_link=False,
    has_working_plan=False,
    recency_days=None,
):
    """A row in the coach's roster list.

    ``suspended`` marks an athlete whose link a downgrade froze (S6 Phase 5): the
    coach keeps read access but can't edit/deliver this athlete's program until back
    within the free cap. It surfaces as a warning badge so the coach sees *which*
    athletes are frozen, not just that they're over the limit. ``demo`` marks a
    one-click-demo athlete (first-time-UX Phase 2) so the row is clearly labeled.
    ``self_link`` marks the coach's own self-coaching row (guided-tour Phase 0) so
    the coach-as-athlete is legible in the list ("You" badge).
    ``has_working_plan`` lets the roster hide the "Draft with AI" CTA for an
    athlete who already has a program (drafting only runs for a fresh plan).
    ``recency_days`` is the athlete's cadence signal — days since their last
    logged (done) session across every live plan (``adherence.
    link_recency_days``) — ``None`` when they have no done logs yet. Programs
    are date-less (§4a, decided 2026-07-18): there is no "current week" to
    measure a percent against, so the roster shows a tone-coded "last trained"
    pill instead of the old per-week compliance meter.
    """
    name = user.display_name()
    meta_parts = [p for p in [_training_label(user)] if p]
    return {
        "id": user.pk,
        "name": name,
        "initials": initials(name),
        "tone": "neutral",
        "meta": " · ".join(meta_parts) or "No training history on file",
        # Contraindications are intentionally absent here (issue #382): they belong
        # on the athlete profile, not as badges cluttering the scannable roster row.
        "recency_label": _recency_label(recency_days),
        "recency_tone": _recency_tone(recency_days),
        "status": "suspended" if suspended else "",
        "status_label": "Suspended" if suspended else "",
        "is_demo": demo,
        "is_self": self_link,
        "has_working_plan": has_working_plan,
    }


def profile_athlete(user):
    """The expanded athlete record behind the roster row."""
    name = user.display_name()
    subtitle_parts = [str(p) for p in [_age(user), _training_label(user)] if p]
    return {
        "id": user.pk,
        "name": name,
        "initials": initials(name),
        "tone": "neutral",
        "subtitle": " · ".join(subtitle_parts) or "No training history on file",
        # Goals are per-plan (D-b); they arrive with the program schema (Phase 2).
        "goals": [],
        "contraindications": [c.text for c in _active_contraindications(user)],
        "has_program": False,
        "recency": "No sessions yet",
        "session_count_14d": 0,
        "status": "",
        "status_label": "",
    }


def _profile_status(link, working_plan, delivered_plan):
    """The program block's status badge — the athlete's most actionable state.

    ``needs_review`` (a pending agent proposal the coach can apply now) outranks
    ``drafting`` (a run still in flight off the request thread), which outranks the
    steady ``delivered``. Scoped to the coach's *own* plans for this athlete — the
    individual working plan and the delivered plan.

    Returns ``(status, label, review_batch_id)`` — the id of the *this athlete's*
    pending batch (newest first) so the profile's "Review agent changes" CTA links
    straight to it, rather than the bare ``review`` redirect that lands on the
    coach's globally-latest pending batch (possibly a different athlete). ``None``
    when there's nothing to review.
    """
    plan_ids = {delivered_plan.pk}
    if working_plan is not None:
        plan_ids.add(working_plan.pk)
    batches = AgentProposalBatch.objects.filter(plan_id__in=plan_ids)
    pending = (
        batches.filter(status=AgentProposalBatch.Status.PENDING)
        .order_by("-created_at")
        .first()
    )
    if pending is not None:
        return "needs_review", "Needs review", pending.pk
    if batches.filter(status=AgentProposalBatch.Status.DRAFTING).exists():
        return "drafting", "Drafting…", None
    return "delivered", "Delivered", None


def _profile_results(link):
    """The athlete's most recent *done* session, scored for the profile card.

    Reuses ``session_results`` (the coach results screen) so the profile's "Latest
    session" card shows the same completion %, RPE-vs-target, and overshoot flag.
    Scoped to the athlete's own *done* logs — a pending "Save progress" draft isn't
    a result — on this link's plans. Archived plans are excluded. ``None`` — the
    card is hidden — when the athlete has no openable logged session yet.
    """
    log = (
        SessionLog.objects.filter(
            session__week__mesocycle__plan__relationship=link,
            athlete=link.athlete,
            status=SessionLog.Status.DONE,
        )
        .exclude(session__week__mesocycle__plan__status=Plan.Status.ARCHIVED)
        .select_related("session__week__mesocycle__plan__relationship")
        .order_by("-date", "-created_at")
        .first()
    )
    if log is None:
        return None
    summary = session_results(log.session)["summary"]
    # The card links to *this* session's results, not the bare ``results``
    # redirect (which lands on the coach's globally-latest logged session —
    # possibly a different athlete).
    summary["session_id"] = log.session_id
    return summary


def _profile_plan(link):
    """The plan behind the athlete-profile program block — newest live plan.

    "Has a program" no longer means "has an ``is_current`` week" (removed
    §4a, decided 2026-07-18); it means the newest (``-modified``) non-archived
    plan that has at least one live week — the same "there's something here
    for the athlete to see" bar the old pointer implied, computable without
    it. An ``Exists`` subquery, not a ``filter(mesocycles__weeks__…)`` join —
    a plan with ZERO weeks still LEFT-JOIN-matches a plain ``…isnull=True``
    filter (the joined row is NULL, and NULL "is null"), which would silently
    let a weekless plan through. Returns ``None`` when the link has no
    qualifying plan.
    """
    live_weeks = Week.objects.filter(
        mesocycle__plan=OuterRef("pk"), deleted_at__isnull=True
    )
    return (
        Plan.objects.filter(relationship=link)
        .exclude(status=Plan.Status.ARCHIVED)
        .filter(Exists(live_weeks))
        .order_by("-modified")
        .first()
    )


def profile_program(link, working_plan):
    """The athlete-profile program block — the athlete's cadence + macrocycle.

    Lights up the long-dead ``has_program`` block (``macrocycle``/
    ``results_summary`` were ``[]``/``None`` placeholders). Programs are
    date-less (§4a, decided 2026-07-18) so there is no "current week" to
    report a position or a percent against — this instead surfaces:

    - ``recency``/``session_count_14d`` — the athlete's cadence (``adherence.
      link_last_trained`` + ``link_session_count``), the same signal behind
      the roster's pill;
    - ``macrocycle`` — the plan's blocks, the rail positioned at the most
      recent DONE log's block (factual history, not a "you are here" claim);
      no block highlights at all if the athlete hasn't logged anything yet on
      *this* plan;
    - ``status`` — needs_review / drafting / delivered (``_profile_status``);
    - ``results_summary`` — the athlete's most recent logged session.

    ``has_program`` is False — the template falls back to the create /
    in-progress empty state — only when there's no live-week plan at all
    (``_profile_plan``). Unlike the old compliance-gated check, it no longer
    depends on there being a *measurable* week: cadence degrades gracefully
    (``None`` -> "No sessions yet") instead of needing to hide the whole
    block. The goal still surfaces from the plan the coach is shaping so the
    left rail isn't blank before any sessions exist.
    """
    plan = _profile_plan(link)
    if plan is None:
        goal = working_plan.goal if working_plan else ""
        return {
            "athlete": {
                "has_program": False,
                "recency": "No sessions yet",
                "session_count_14d": 0,
                "status": "",
                "status_label": "",
                "review_batch_id": None,
                "goals": [goal] if goal else [],
            },
            "macrocycle": [],
            "results_summary": None,
        }

    last_log = adherence.link_last_trained(link)
    # Only highlight a block on THIS plan's rail — a most-recent log that
    # belongs to a different (older, still-live) plan has nothing to
    # highlight here.
    highlight_mesocycle = None
    if last_log is not None and last_log.session.week.mesocycle.plan_id == plan.pk:
        highlight_mesocycle = last_log.session.week.mesocycle
    mesocycles = list(plan.mesocycles.all())
    states = _phase_states(mesocycles, highlight_mesocycle)
    macrocycle = [serialize_mesocycle(m, s) for m, s in zip(mesocycles, states)]
    status, status_label, review_batch_id = _profile_status(link, working_plan, plan)
    # The goal of the plan the coach is actively shaping if there is one, else
    # the delivered plan's.
    goal = (working_plan.goal if working_plan else "") or plan.goal
    recency = (
        "No sessions yet" if last_log is None else _relative_when(last_log.created_at)
    )
    return {
        "athlete": {
            "has_program": True,
            "recency": recency,
            "session_count_14d": adherence.link_session_count(link, days=14),
            "status": status,
            "status_label": status_label,
            "review_batch_id": review_batch_id,
            "goals": [goal] if goal else [],
        },
        "macrocycle": macrocycle,
        "results_summary": _profile_results(link),
    }


# -- personal records panel (Phase 4d) -------------------------------------
#
# The persistent "records book" — best e1RM per lift with provenance — shared by
# the athlete's training home and the coach's athlete-profile, both fed by the 4b
# derive-on-read engine (``personal_records``); nothing is persisted. Unit is a
# per-PLAN property (there is no athlete-level preference), so each host scopes to
# a single plan's unit rather than pooling kg and lb: the athlete's / this link's
# most-recently-active plan. An empty row list hides the panel (the templates
# guard), so a lifter with no numeric-parseable best sees no empty chrome.


def _personal_record_rows(athlete, unit):
    """Best-lift display rows for a records panel — alphabetical by lift.

    Each row carries the estimated 1RM (formatted like the stored ``AthleteOneRm``
    display via ``_fmt_num``) and the winning set's provenance (reps/load/date).
    """
    rows = [
        {
            "name": r.name,
            "e1rm": _fmt_num(round(r.e1rm, 2)),
            "unit": r.unit,
            "reps": r.reps,
            "load": r.load,
            "date": r.date,
        }
        for r in personal_records(athlete, unit=unit).values()
    ]
    rows.sort(key=lambda row: row["name"].lower())
    return rows


def _records_unit_plan(plans):
    """The plan whose unit denominates a records panel — most recently active.

    ``personal_records`` is unit-scoped, so a panel shows one denomination; pick
    the most-recently-modified non-archived plan (the same "what they're on now"
    heuristic the home card ordering uses). ``None`` when there's none — the panel
    is then empty.
    """
    return plans.exclude(status=Plan.Status.ARCHIVED).order_by("-modified").first()


def athlete_personal_records(user):
    """The athlete's records panel for their training home (Phase 4d)."""
    plan = _records_unit_plan(Plan.objects.for_athlete(user))
    if plan is None:
        return {"rows": [], "unit": ""}
    return {"rows": _personal_record_rows(user, plan.unit), "unit": plan.unit}


def coach_personal_records(link):
    """The athlete's records panel for the coach's athlete-profile (Phase 4d).

    The bests are unit-scoped and athlete-global (D4: PRs ride the structured
    ``LoggedSet``, not per-coach); the coach reaches this only through an active
    link and sees them in their own plan's unit.
    """
    plan = _records_unit_plan(Plan.objects.filter(relationship=link))
    if plan is None:
        return {"rows": [], "unit": ""}
    return {"rows": _personal_record_rows(link.athlete, plan.unit), "unit": plan.unit}


def _relative_when(dt):
    """A compact "N ago" label for the activity feed (coarsest unit only).

    ``humanize`` isn't installed, so this trims ``timesince`` to its leading
    unit — "2 days, 3 hours" → "2 days ago" — and collapses a just-now log
    ("0 minutes") to a friendly "just now".
    """
    coarse = timesince(dt).split(",")[0].strip()
    if not coarse or coarse.startswith("0"):
        return "just now"
    return f"{coarse} ago"


def roster_activity(coach, *, limit=8):
    """The coach's recent-activity feed — athletes' latest completed sessions.

    Lights up the roster's long-dead ``activity`` placeholder: each event names
    the athlete, the session they logged, and how long ago. Scoped to the coach's
    active links and *done* logs by ``adherence.recent_logs``.
    """
    events = []
    for log in adherence.recent_logs(coach, limit=limit):
        name = log.athlete.display_name()
        session_label = log.session.name or f"Day {log.session.day_number}"
        events.append(
            {
                "athlete": {
                    "id": log.athlete.pk,
                    "name": name,
                    "initials": initials(name),
                    "tone": "neutral",
                },
                "kind": "log",
                "text": f"logged {session_label}",
                "when": _relative_when(log.created_at),
            }
        )
    return events


def pending_invite(invite):
    """An outstanding email-invite row in the coach's roster (N4 onboarding).

    Carries ``is_expired`` (Phase 3) so the row reads "Expired" instead of
    "Pending" once the TTL runs out — either swept to ``expired`` status or merely
    past due. Both states offer Resend.
    """
    return {
        "email": invite.email,
        "token": invite.token,
        "when": invite.created_at,
        "is_expired": invite.status == CoachInvite.Status.EXPIRED or invite.is_expired,
    }


def pending_request(link):
    """A pending athlete→coach request row in the coach's roster (N4 Phase 2).

    The coach accepts/declines via the recipient token views, so the row carries
    the link's ``token`` to address them.
    """
    name = link.athlete.display_name()
    return {
        "name": name,
        "initials": initials(name),
        "token": link.token,
        "when": link.created_at,
    }


#: Human labels for the relationship-history surface, keyed by terminal/pending
#: status. ``PENDING_COACH_INVITE`` here is a *re-invite* awaiting the athlete.
_HISTORY_STATUS_LABELS = {
    CoachAthlete.Status.ENDED: "Ended",
    CoachAthlete.Status.DECLINED: "Declined",
    CoachAthlete.Status.PENDING_COACH_INVITE: "Awaiting response",
}


def relationship_history(coach):
    """The coach's relationship history: past athletes + pending re-invites.

    A coach (or athlete) who ends a relationship — or declines an invite/request —
    drops the ``CoachAthlete`` row to a terminal status, so it leaves the active
    roster though the row + its archived plans persist. This surfaces those rows
    in one query (mirroring ``athlete_pending``'s split):

    - ``past`` — ended/declined links, newest-closed first, each re-invitable;
    - ``reconnecting`` — re-invites awaiting the athlete's response (a coach-side
      ``pending_coach_invite``, which the athlete sees on their training home and
      which is surfaced nowhere else).

    Demo relationships are excluded — history is about real past clients. So is
    an ended self-link: the roster's "Add yourself as an athlete" affordance is
    its reopen path, not a "re-invite" (you can't re-invite yourself).
    """
    links = (
        CoachAthlete.objects.for_coach(coach)
        .exclude(is_demo=True)
        .exclude(is_self=True)
        .filter(status__in=list(_HISTORY_STATUS_LABELS))
        .select_related("athlete")
    )
    past, reconnecting = [], []
    for link in links:
        name = link.athlete.display_name()
        row = {
            "id": link.athlete_id,
            "name": name,
            "initials": initials(name),
            "token": link.token,
            "status": link.status,
            "status_label": _HISTORY_STATUS_LABELS[link.status],
        }
        if link.is_closed:
            # ``closed_at`` is set for any link closed through the state machine;
            # fall back to ``created_at`` for a hand-written/legacy row.
            row["when"] = link.closed_at or link.created_at
            past.append(row)
        else:
            # A re-invite reopens the row in place, so no field records *when* the
            # re-invite was sent (``created_at`` is the original link date). The
            # reconnecting surface shows state ("awaiting reply"), not a date, and
            # orders by ``created_at`` only for a stable, deterministic sequence.
            reconnecting.append((link.created_at, row))
    past.sort(key=lambda r: r["when"], reverse=True)
    reconnecting.sort(key=lambda pair: pair[0], reverse=True)
    return {"past": past, "reconnecting": [row for _, row in reconnecting]}


def agent_allowance(coach):
    """The AI-agent meter for the designer + roster card (S6 Phase 5; flat plan D14).

    Under the flat monthly Pro plan every tier is metered except ``comped``: a free
    coach gets ``FREE_AGENT_ALLOWANCE`` runs/month, a trialing/active coach the
    larger ``PAID_AGENT_ALLOWANCE``, and only a comped coach (owner/demo) is
    unlimited. ``metered`` is True for the capped tiers (the UI shows "N of M agent
    runs left") and False for comped (nothing shown). ``tier`` (``free`` / ``paid``
    / ``unlimited``) lets a template pick the copy — a *free* coach's exhausted-CTA
    offers an upgrade; a *paid* coach's just notes the monthly reset (no higher tier
    to sell). ``can_use`` mirrors ``access.can_use_agent`` so a template drives the
    composer/CTA off this one read without a second query.
    """
    cap = billing_access.agent_allowance(coach)  # None = uncapped (comped)
    if cap is None:
        return {
            "metered": False,
            "allowance": 0,
            "used": 0,
            "remaining": None,
            "can_use": True,
            "tier": "unlimited",
        }
    remaining = billing_access.agent_runs_remaining(coach)
    return {
        "metered": True,
        "allowance": cap,
        "used": cap - remaining,
        "remaining": remaining,
        "can_use": remaining > 0,
        "tier": "paid" if billing_access.is_active(coach) else "free",
    }


def billing_state(coach):
    """The coach's billing/paywall state for the roster (S6 Phase 3).

    A template-friendly read over ``billing/access.py`` + the subscription row:
    the tier, seat usage, and which upgrade CTAs to offer. The free tier sees
    "start your no-card trial" (single-use) and "subscribe"; a coach with a real
    Stripe subscription sees "manage billing" (the hosted Portal); an over-limit
    coach (post-downgrade, D6) sees the freeze warning naming how many athletes are
    suspended (``suspended_count``, S6 Phase 5). ``seat_limit`` is ``None`` for an
    unlimited (active/trial/comped) coach so the template hides the cap.
    """
    sub = getattr(coach, "coach_subscription", None)
    status = sub.status if sub else CoachSubscription.Status.FREE
    seat_limit = billing_access.effective_seat_limit(coach)
    active = billing_access.is_active(coach)
    # The no-card trial is single-use: offer it only to a free coach who has never
    # trialed (no row, or a row whose ``trial_end`` was never set).
    can_start_trial = (
        not active
        and status == CoachSubscription.Status.FREE
        and (sub is None or sub.trial_end is None)
    )
    return {
        "status": status,
        "status_label": CoachSubscription.Status(status).label,
        "is_active": active,
        "on_trial": active and status == CoachSubscription.Status.TRIALING,
        "trial_end": sub.trial_end if sub else None,
        "seat_count": billing_access.active_seat_count(coach),
        "seat_limit": None if seat_limit == math.inf else int(seat_limit),
        "can_add_athlete": billing_access.can_add_athlete(coach),
        "can_use_agent": billing_access.can_use_agent(coach),
        "agent": agent_allowance(coach),
        "over_limit": billing_access.is_over_limit(coach),
        # How many active athletes are soft-suspended by the downgrade (S6 Phase 5):
        # 0 unless over the limit, then the count beyond the oldest free cap.
        "suspended_count": len(billing_access.suspended_athlete_ids(coach)),
        "can_start_trial": can_start_trial,
        "has_stripe_subscription": bool(sub and sub.stripe_subscription_id),
        "price_summary": PRICE_SUMMARY,
    }


def coach_billing(coach):
    """The coach-facing billing & usage page context (agent-usage — coach surface).

    The complement to the staff-only owner dashboard (``usage_dashboard``): that
    shows org-wide **cost** (COGS); this shows *one coach* their **bill** (the flat
    monthly Pro price they owe, D14) and **how much agent they've used** this month,
    broken down per athlete. The hard line: a coach sees what they pay and how
    much they've used, **never** the internal per-run cost estimate, so this context
    carries run counts and the flat price only — no ``cost``/``margin`` keys.

    The month window is the report's current calendar month, the same window the
    agent meter counts against, so ``runs_this_month`` reconciles with the allowance
    in ``state["agent"]``.
    """
    state = billing_state(coach)
    start, end = agent_usage_report.current_month_bounds()
    breakdown = agent_usage_report.coach_run_breakdown(coach, start=start, end=end)
    return {
        "state": state,
        "plan_price": agent_usage_report.PRO_PRICE_USD,
        "seats": state["seat_count"],
        "runs_this_month": sum(row.runs for row in breakdown),
        "breakdown": breakdown,
        "month_label": start.strftime("%B %Y"),
    }


def athlete_pending(user):
    """Pending coach links the athlete sees on their training home (N4 Phase 2).

    Splits the athlete's pending links into ``invites`` (a coach invited them —
    they accept/decline) and ``requests`` (they asked a coach — awaiting, with a
    withdraw). Each row names the coach and carries the link ``token`` for the
    accept/decline/withdraw forms.
    """
    links = (
        CoachAthlete.objects.for_athlete(user)
        .pending()
        .select_related("coach")
        .order_by("-created_at")
    )
    invites, requests = [], []
    for link in links:
        name = link.coach.display_name()
        row = {
            "coach": name,
            "initials": initials(name),
            "token": link.token,
            "when": link.created_at,
        }
        if link.status == CoachAthlete.Status.PENDING_COACH_INVITE:
            invites.append(row)
        else:
            requests.append(row)
    return {"invites": invites, "requests": requests}


def deliver_screen(plan, week=None):
    """Context for the plan-bound deliver screen (P3; block delivery).

    2d (parity plan §3.3): the athlete already sees every edit live — delivering
    is a one-time heads-up + a ``WeekDelivery`` history snapshot, not a release.
    The "changes since last delivery" diff below is an optional what-changed
    view for the coach, never a gate.

    The individual deliver path nudges about the **whole block** — one
    ``Mesocycle``, every one of its live weeks — at once (see ``plan_deliver``),
    so this screen confirms the block, not a single week. The ``week`` argument
    (the plan's
    current/live week by default, or an explicit ``?week=`` the coach picked in
    the designer's switcher) only *selects which block* to send; ``block`` is
    that week's mesocycle.

    ``deliver["weeks"]`` carries one entry per live week of the block, each with
    its OWN "changes since last delivery" diff: a re-delivered week (it has a
    prior ``WeekDelivery``) diffs its live grid against its latest snapshot
    (``diff_week_snapshots``), a never-delivered week has ``changes=None``. The
    block-level ``is_redelivery`` / ``has_changes`` fold those per-week facts up
    for the headline. ``week_id`` stays the target week's pk — the Alpine
    ``mesoDeliver(planId, csrf, weekId)`` component still posts it to pick the
    block. Scheduling stays a later-slice concern.
    """
    live = current_week(plan)
    target = week or live
    # The target week only *selects the block*; block delivery sends all its live
    # weeks, so there's no "sending a week that isn't live" warning any more.
    block = target.mesocycle if target else None

    weeks = []
    block_is_redelivery = False
    block_has_changes = False
    if block is not None:
        # Live weeks only (soft delete, designer framework Phase 0): a removed
        # week is never delivered (the POST 404s it), so it's not listed here.
        for w in block.weeks.filter(deleted_at__isnull=True).order_by("index"):
            # Live rows only: a removed day doesn't count toward "N sessions".
            session_count = w.sessions.filter(deleted_at__isnull=True).count()
            is_redelivery = w.deliveries.exists()
            # On a re-delivery, diff the week's live grid against the snapshot it
            # last went out as, so the coach sees what's about to change for the
            # athlete. A first delivery (no prior snapshot) has nothing to diff.
            changes = None
            if is_redelivery:
                last_payload = (
                    WeekDelivery.objects.filter(week=w)
                    .order_by("-delivered_at")
                    .values_list("payload", flat=True)
                    .first()
                )
                if last_payload:
                    changes = diff_week_snapshots(
                        serialize_week_snapshot(w), last_payload
                    )
            if is_redelivery:
                block_is_redelivery = True
            if changes is not None and changes["has_changes"]:
                block_has_changes = True
            weeks.append(
                {
                    "id": w.pk,
                    "label": f"Wk {w.index}",
                    "index": w.index,
                    "is_delivered": w.delivered_at is not None,
                    "session_count": session_count,
                    "is_redelivery": is_redelivery,
                    "changes": changes,
                }
            )

    week_count = len(weeks)
    athlete = profile_athlete(plan.athlete)
    athlete["block"] = block.name if block else ""
    athlete["week"] = f"{week_count} week{'' if week_count == 1 else 's'}"
    return {
        "athlete": athlete,
        "deliver": {
            "what": plan.title,
            "block_name": block.name if block else "",
            "week_count": week_count,
            "is_redelivery": block_is_redelivery,
            "has_changes": block_has_changes,
            "week_id": target.pk if target else None,
            "weeks": weeks,
        },
    }


def review_changes(batch):
    """Context for the review screen from a real ``AgentProposalBatch`` (B6).

    Feeds the same template the prototype fixtures did, so a real batch renders
    unchanged; per-change approve/reject persistence + apply land in Phase 2.
    """
    plan = batch.plan
    return {
        "athlete": {"name": plan.athlete.display_name()},
        "changes": [serialize_proposed_change(c) for c in batch.changes.all()],
    }


def coach_style(coach):
    """The current coach's programming voice, for the profile left rail."""
    profile = getattr(coach, "coach_profile", None)
    if profile is None:
        return {"tags": [], "avoid": ""}
    return {"tags": profile.programming_style or [], "avoid": profile.avoid_rules}


# -- session results (athlete slice Phase 3) -------------------------------
#
# The coach's results screen, off real logs (``mockdata.RESULTS_*`` retired):
# the athlete's most recent ``SessionLog`` for a session, scored against the
# prescribed targets — completion, RPE vs target, and the flags that drive the
# "adjust next week" hand-off to the agent. The same logged truth the agent
# grounds on (``serialize_recent_logs``), now shown to the coach.

# A logged set running this many RPE points over target is worth acting on — it
# becomes a "flag" (the row still lights up for *any* overshoot).
RPE_FLAG_THRESHOLD = 1.0


def _text_label(text):
    """A one-line display label for a freeform cell's text (Phase 2a).

    The coach's own notation IS the target label now — verbatim, with a
    multi-line cell folded to `` · `` separators; "—" for a blank cell.
    """
    parts = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " · ".join(parts) or "—"


def _results_target_label(prescription, recovered_rpe=None):
    """The prescribed target — the cell's freeform text, verbatim.

    ``recovered_rpe`` is appended (``3 x 12`` -> ``3 x 12, RPE 9``) when the
    target RPE used for the over-target flag/``avg_rpe_delta`` came from a
    coach sub-line rather than line 0 itself (see ``_sub_line_rpe``) — never
    when line 0 already carries its own inline RPE, since that's already
    visible in the verbatim text. Without this, a coach who follows the
    intended model (RPE on its own sub-line — spreadsheet-parity plan
    §2.3/§2.6) would see a target with no RPE sitting right next to a flag
    computed from an RPE the label never showed — incoherent. Callers must
    only pass a value here that line 0 didn't already yield (see
    ``_exercise_result``/``_avg_rpe_delta``'s callers) — this function does
    not re-check that itself.
    """
    label = _text_label(prescription.text)
    if not recovered_rpe:
        return label
    return f"RPE {recovered_rpe}" if label == "—" else f"{label}, RPE {recovered_rpe}"


def _logged_label(logged_sets, unit):
    """What the athlete did, e.g. "2×12, 1×9 @ 41 kg" (reps grouped, load suffixed).

    Equal-rep runs collapse to ``count×reps``; a uniform load is appended once,
    a varying numeric load as a range. A non-numeric load ("BW") carries no unit.
    """
    groups = []  # [count, reps] runs, in logged order
    for s in logged_sets:
        reps = s.reps or "—"
        if groups and groups[-1][1] == reps:
            groups[-1][0] += 1
        else:
            groups.append([1, reps])
    label = ", ".join(f"{count}×{reps}" for count, reps in groups)
    loads = [s.load for s in logged_sets if s.load]
    if loads and all(x == loads[0] for x in loads):
        suffix = f" {unit}" if _num(loads[0]) is not None else ""
        label += f" @ {loads[0]}{suffix}"
    elif loads and all(_num(x) is not None for x in loads):
        nums = [_num(x) for x in loads]
        label += f" @ {_fmt_num(min(nums))}–{_fmt_num(max(nums))} {unit}"
    return label


def _worst_rep_shortfall(prescription, logged_sets):
    """The biggest rep miss vs the prescribed reps, as ``(deficit, set_number)``.

    Only meaningful when the prescribed reps parse to a plain number (not
    "AMRAP" / "8-10" — text-first, the reps come from parsing the freeform
    cell); returns None otherwise, or when every logged set met the target.
    Catches the case a set-count check misses — all sets done, but reps fell
    short on one (e.g. a 3×12 logged as 12, 12, 9).
    """
    parsed = prescription.parsed() or {}
    target_reps = _num(parsed.get("reps"))
    if target_reps is None:
        return None
    worst = None
    for s in logged_sets:
        reps = _num(s.reps)
        if reps is None or reps >= target_reps:
            continue
        deficit = target_reps - reps
        if worst is None or deficit > worst[0]:
            worst = (deficit, s.set_number)
    return worst


def _coach_sub_lines_by_slot(session):
    """This session's coach-authored sub-lines (line >= 1), keyed by exercise_slot id.

    One query for the whole session (``Session.line_cells()``, already used
    this way by ``serialize_session``) rather than one per prescription —
    batched, matching the lookup ``seed_meso_demo.py``'s
    ``_logged_sets_from_cells`` uses for the analogous (but cross-session)
    case. A single session's cells all share one week, so grouping by slot id
    alone is enough here (no need for that helper's ``(slot, week)`` key).

    Athlete-authored lines (the athlete's own freeform tracking sub-rows —
    ``135lbs x 12``, Phase 4a) are excluded: ``athlete_authored`` is stamped
    per (slot, week, line) row by ``athlete_cell_write``/``cell_line_write``
    (views.py) and flipped back on a coach edit, so it reliably marks exactly
    the rows the *athlete* wrote — never the coach's own prescription, which
    is what a results target must be derived from.
    """
    by_slot = defaultdict(list)
    for line_cell in session.line_cells():
        if not line_cell.athlete_authored:
            by_slot[line_cell.exercise_slot_id].append(line_cell)
    return by_slot


def _sub_line_rpe(prescription, sub_lines_by_slot):
    """The first RPE recovered from this cell's coach sub-lines, or None.

    Walks the row's sub-lines in stack order (``_coach_sub_lines_by_slot``
    sorts by ``line``) for the first that parses an RPE — the intended model
    per spreadsheet-parity plan §2.3/§2.6, an "RPE row directly beneath the
    prescription" reached by arrow-down. Only ever consulted when line 0
    itself carries none (see the callers) — line 0's inline RPE always wins.
    """
    for sub_line in sub_lines_by_slot.get(prescription.exercise_slot_id, ()):
        rpe = (sub_line.parsed() or {}).get("rpe")
        if rpe:
            return rpe
    return None


def _exercise_result(prescription, logged_sets, unit, sub_lines_by_slot):
    """One results row + its RPE overshoot (None when not comparable).

    The row mirrors the prototype's columns (target / logged / RPE / note); the
    RPE shown is the *hardest* logged set, and ``rpe_state`` lights "over" on any
    overshoot. The note is the most actionable fact we can derive, in order: a
    set shortfall, a rep shortfall (the prescribed reps missed on a set), then a
    meaningful RPE overshoot.
    """
    parsed = prescription.parsed() or {}
    line0_rpe = parsed.get("rpe")
    # Line 0 wins when present (hand-authored cells that pack RPE inline are
    # unaffected); otherwise recover it from a coach sub-line — and only then
    # is it new information the label needs to surface (issue #487).
    recovered_rpe = (
        None if line0_rpe else _sub_line_rpe(prescription, sub_lines_by_slot)
    )
    target_rpe = _num(line0_rpe or recovered_rpe)
    logged_rpes = [_num(s.rpe) for s in logged_sets if _num(s.rpe) is not None]
    top_rpe = max(logged_rpes) if logged_rpes else None
    overshoot = (
        top_rpe - target_rpe if top_rpe is not None and target_rpe is not None else None
    )
    prescribed_n = _prescribed_set_count(prescription)
    logged_n = len(logged_sets)
    rep_short = _worst_rep_shortfall(prescription, logged_sets)
    if prescribed_n and 0 < logged_n < prescribed_n:
        note = f"{logged_n}/{prescribed_n} sets logged"
    elif rep_short is not None:
        deficit, set_number = rep_short
        plural = "s" if deficit != 1 else ""
        note = f"missed {_fmt_num(deficit)} rep{plural} on set {set_number}"
    elif overshoot is not None and overshoot >= RPE_FLAG_THRESHOLD:
        note = f"RPE {_fmt_num(top_rpe)} over target"
    else:
        note = ""
    row = {
        "name": prescription.name,
        "target": _results_target_label(prescription, recovered_rpe),
        "logged": _logged_label(logged_sets, unit) if logged_sets else "—",
        "rpe": _fmt_num(top_rpe) if top_rpe is not None else "—",
        "rpe_state": "over" if overshoot is not None and overshoot > 0 else "on",
        "note": note,
    }
    return row, overshoot


def _avg_rpe_delta(prescriptions, sets_by_prescription, sub_lines_by_slot):
    """Mean (logged − target) RPE across comparable sets, signed; "—" if none.

    Target RPE is line 0's inline value if present, else recovered from a
    coach sub-line (``_sub_line_rpe`` — issue #487) — the same effective
    target ``_exercise_result`` derives per row.
    """
    deltas = []
    for prescription in prescriptions:
        parsed = prescription.parsed() or {}
        target = _num(
            parsed.get("rpe") or _sub_line_rpe(prescription, sub_lines_by_slot)
        )
        if target is None:
            continue
        for s in sets_by_prescription.get(prescription.pk, []):
            logged = _num(s.rpe)
            if logged is not None:
                deltas.append(logged - target)
    if not deltas:
        return "—"
    avg = sum(deltas) / len(deltas)
    return "0.0" if round(avg, 1) == 0 else f"{avg:+.1f}"


def _session_label(session):
    week = session.week
    label = f"Wk {week.index} · Day {session.day_number}"
    return f"{label} — {session.name}" if session.name else label


def _logged_date(log):
    if log is None or log.date is None:
        return None
    return f"{log.date:%a, %b} {log.date.day}"


def session_results(session):
    """The coach's results screen for one session, off the athlete's real log.

    Reads the athlete's most recent *done* ``SessionLog`` for ``session`` and
    scores its sets against the prescribed targets. A pending draft (the athlete
    hit "Save progress" but hasn't finished) is not feedback yet, so it — like an
    unlogged session — renders an honest awaiting state (targets only, 0%
    complete) rather than inventing numbers. ``session`` arrives coach-scoped;
    its cells are read via ``session.cells()`` (P0 fixed-lineup cutover).
    """
    plan = session.week.mesocycle.plan
    athlete = plan.athlete
    prescriptions = list(session.trainable_cells())
    sub_lines_by_slot = _coach_sub_lines_by_slot(session)
    log = (
        SessionLog.objects.filter(
            session=session, athlete=athlete, status=SessionLog.Status.DONE
        )
        .order_by("-date", "-created_at")
        .prefetch_related("sets")
        .first()
    )
    sets_by_prescription = defaultdict(list)
    if log is not None:
        for s in log.sets.all():
            if s.prescription_id is not None:
                sets_by_prescription[s.prescription_id].append(s)

    results = [
        _exercise_result(
            p, sets_by_prescription.get(p.pk, []), plan.unit, sub_lines_by_slot
        )
        for p in prescriptions
    ]
    rows = [row for row, _ in results]

    # New personal records (Phase 4c): lifts in this logged session that beat the
    # athlete's prior best e1RM — DONE-only, so a pending draft or unlogged session
    # yields none. Flag the matching row (by the same B4 lift identity the engine
    # keys on) so the coach sees which lift PR'd, and hand the list to the summary
    # for the celebration callout.
    new_records = new_records_in(log) if log is not None else []
    pr_keys = {r.key for r in new_records}
    for row, p in zip(rows, prescriptions):
        row["pr"] = key_str(p.exercise_id, p.name) in pr_keys

    # Completion = logged sets / prescribed sets. A free-form set cell ("AMRAP",
    # "3-4") has no integer target, so fall back to what was logged for that row
    # — it neither divides by zero nor skews the ratio with an empty denominator.
    prescribed_total = 0
    logged_total = 0
    for p in prescriptions:
        logged_n = len(sets_by_prescription.get(p.pk, []))
        prescribed_total += _prescribed_set_count(p) or logged_n
        logged_total += logged_n
    completion = (
        min(round(100 * logged_total / prescribed_total), 100)
        if prescribed_total
        else 0
    )

    flagged = [
        (row, o) for row, o in results if o is not None and o >= RPE_FLAG_THRESHOLD
    ]
    if flagged:
        worst_row, worst_over = max(flagged, key=lambda pair: pair[1])
        flag = (
            f"{worst_row['name']} ran {_fmt_num(worst_over)} RPE over target "
            "— consider holding load next session."
        )
    else:
        flag = ""

    return {
        "athlete": {"name": athlete.display_name()},
        "plan_id": plan.pk,
        "rows": rows,
        "summary": {
            "session": _session_label(session),
            "logged": _logged_date(log),
            "completion": completion,
            "avg_rpe_delta": _avg_rpe_delta(
                prescriptions, sets_by_prescription, sub_lines_by_slot
            ),
            "flag": flag,
            "flag_count": len(flagged),
            "logged_state": log is not None,
            "new_records": [serialize_new_record(r) for r in new_records],
        },
    }


# -- athlete surface (athlete slice Phase 1) -------------------------------
#
# The athlete's *own* read view (distinct from the coach's view of an athlete).
# Scoped to live weeks across the athlete's active coaches (2d: edits are live —
# delivery never gates visibility). Log status comes only from the athlete's
# *own* ``SessionLog`` rows.


def _done_session_ids(session_ids, athlete):
    """Which of ``session_ids`` the athlete has a *done* log for (one query)."""
    return set(
        SessionLog.objects.filter(
            session_id__in=session_ids,
            athlete=athlete,
            status=SessionLog.Status.DONE,
        ).values_list("session_id", flat=True)
    )


def _athlete_session_row(session, *, done):
    """One session in the athlete's week — a tappable row on the home screen."""
    status = "done" if done else "pending"
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        # Trainable rows only — live + non-skipped (P0 fixed-lineup cutover); a
        # week-skipped exercise doesn't count toward the day's "N exercises" chip.
        "exercise_count": session.trainable_cells().count(),
        "status": status,
        "status_label": "Logged" if done else "To do",
        "url": reverse("meso:athlete_session", kwargs={"pk": session.pk}),
    }


def _cell_summary(cell):
    """A read-only prescription summary for one athlete-table cell.

    Reads a ``serialize_mesocycle_grid`` cell dict (the athlete table is
    transformed from that coach grid, not from ``Prescription`` rows): the
    freeform ``text`` verbatim (Phase 2a), with any non-blank sub-lines folded
    in after it so the athlete sees the whole stack.
    """
    parts = [cell["text"]]
    parts.extend(line["text"] for line in cell.get("lines", ()))
    return _text_label("\n".join(p for p in parts if p and p.strip()))


def _athlete_block_grid(block, focus_week_id):
    """The athlete's read-only multi-week table, transformed from the coach grid.

    Reuses ``serialize_mesocycle_grid`` (one dense query set for the whole block —
    no N+1 per cell) and strips it to what a read-only table needs: every live
    week of the block as a column (2d: delivery no longer gates visibility —
    the athlete sees the block exactly as it stands), each cell reduced to a
    display summary (the freeform text stack; em-dash rendered by the template
    when ``skipped``), and every coach-editing internal (history / ``*_id`` /
    ``prescription_id`` / ``session_id``) dropped. The card's anchored column
    (``focus_week_id`` — the derived scroll hint, or the ``?week=`` override;
    see ``athlete_home``) is flagged (``focused``) so the template can give it
    a light highlight — never a "you are here" claim, since the app doesn't
    make one (docs/meso/remove-current-week-plan.md).
    """
    grid = serialize_mesocycle_grid(block)
    columns = grid["weeks"]
    col_keys = [str(w["id"]) for w in columns]
    weeks = [
        {
            "index": w["index"],
            "label": w["label"],
            "deload": w["deload"],
            "focused": w["id"] == focus_week_id,
        }
        for w in columns
    ]
    days = []
    for day in grid["days"]:
        rows = []
        for row in day["rows"]:
            cells = []
            # Include the row only if the athlete actually trains it in some
            # week — a row skipped in every column (e.g. an "add this week
            # only" exercise whose other weeks are placeholder cells) would
            # render as a name beside a strip of em-dashes, so gate on a
            # non-skipped cell somewhere.
            has_trainable = False
            for w, key in zip(columns, col_keys):
                cell = row["cells"].get(key)
                focused = w["id"] == focus_week_id
                if cell is None:
                    cells.append({"present": False, "focused": focused})
                    continue
                if not cell["skipped"]:
                    has_trainable = True
                cells.append(
                    {
                        "present": True,
                        "focused": focused,
                        "summary": _cell_summary(cell),
                        "skipped": cell["skipped"],
                    }
                )
            if has_trainable:
                rows.append({"name": row["name"], "cells": cells})
        if rows:
            days.append(
                {
                    "name": day["name"],
                    "bias": day["bias"],
                    "day_number": day["day_number"],
                    "rows": rows,
                }
            )
    return {"weeks": weeks, "days": days}


def _scroll_hint(plan_weeks, user):
    """The card's derived, re-read-on-every-request scroll position.

    The whole replacement for the removed ``is_current`` pointer (docs/meso/
    remove-current-week-plan.md §5, decided 2026-07-18): the last live week
    (plan order) containing any of THIS athlete's own logged sessions, else
    the plan's earliest live week. No stored pointer — nothing to advance,
    guard, or snapshot for undo. It's rendered as neutral scroll-restore
    ("back to where you last trained"), never a "you are here" claim, so it
    carries no special label or heading in the template — only a light
    ``focused`` highlight shared with the ``?week=`` override.
    """
    logged_week_ids = set(
        SessionLog.objects.filter(
            athlete=user, session__week_id__in=[w.pk for w in plan_weeks]
        ).values_list("session__week_id", flat=True)
    )
    return next(
        (w for w in reversed(plan_weeks) if w.pk in logged_week_ids),
        plan_weeks[0],
    )


def _athlete_default_plan_id(user, plans):
    """Which of the athlete's cards leads the list (§5, decided 2026-07-18).

    "Last-opened" isn't tracked (no new storage — a per-user-per-plan
    write-on-read timestamp was considered and skipped). The strongest
    zero-storage engagement signal is the plan holding the athlete's
    most-recently-written ``SessionLog`` (any status — even a "Save
    progress" draft is real engagement, not just a completed one). Falls
    back to the most-recently-*delivered* plan (``max Week.delivered_at``)
    for an athlete who has never logged, then to ``-modified`` (``plans``'
    own order, so a plan neither logged into nor delivered still gets a
    deterministic, already-computed answer). ``None`` when ``plans`` is
    empty.
    """
    if not plans:
        return None
    plan_ids = [p.pk for p in plans]
    most_recent_log = (
        SessionLog.objects.filter(
            athlete=user, session__week__mesocycle__plan_id__in=plan_ids
        )
        .select_related("session__week__mesocycle")
        .order_by("-created_at")
        .first()
    )
    if most_recent_log is not None:
        return most_recent_log.session.week.mesocycle.plan_id
    most_delivered_plan_id = (
        Week.objects.filter(
            mesocycle__plan_id__in=plan_ids,
            deleted_at__isnull=True,
            delivered_at__isnull=False,
        )
        .order_by("-delivered_at")
        .values_list("mesocycle__plan_id", flat=True)
        .first()
    )
    if most_delivered_plan_id is not None:
        return most_delivered_plan_id
    return plans[0].pk


def athlete_home(user, focus_week_id=None):
    """The athlete's active programs, each opened onto a derived scroll hint.

    One card per non-archived plan across the athlete's *active* coaches (D-a).
    Edits are live (2d, parity plan §3.3): the athlete sees every live week of
    the plan the moment the coach types it — delivery is a heads-up + snapshot,
    never a visibility gate. A plan with no weeks yet is shown as awaiting;
    otherwise the card opens onto ``_scroll_hint`` — that week's sessions are
    the tappable log rows — and carries a read-only multi-week table
    (``grid``) of the whole block so the athlete can see the weeks around it
    (P3). The app never asserts a "you are here" position (docs/meso/remove-
    current-week-plan.md): the scroll hint is derived fresh on every read, not
    a stored/advanced pointer, and it's never labeled as the athlete's
    position — see ``_scroll_hint``.

    ``focus_week_id`` is a **display-only** override (issue #456): when it names
    a live week belonging to one of the athlete's own cards, that week — and
    therefore its block — becomes the anchor for THAT plan's card only; every
    other card renders its own derived scroll hint. An invalid/foreign/deleted
    id is silently ignored, rendering exactly as a bare request would.

    The card list itself leads with the athlete's most-recently-engaged
    program (``_athlete_default_plan_id``) rather than the DB's plain
    ``-modified`` order, which a *coach's* edit can bump regardless of
    athlete activity.

    The chip strip spans every live week of the whole PLAN, not just the
    anchored block (Finding 1, issue #456): a coach adding a new block must
    stay reachable by tapping a chip. ``grid``/``sessions`` stay block-scoped
    — they follow the anchor (the scroll hint's mesocycle) alone.
    """
    requested_week = None
    if focus_week_id is not None:
        requested_week = (
            Week.objects.filter(pk=focus_week_id, deleted_at__isnull=True)
            .select_related("mesocycle")
            .first()
        )
    plans = list(
        Plan.objects.for_athlete(user)
        .exclude(status=Plan.Status.ARCHIVED)
        .select_related("relationship__coach")
        .order_by("-modified")
    )
    default_plan_id = _athlete_default_plan_id(user, plans)
    cards = []
    for plan in plans:
        # Every live week of the plan, in plan order — the same
        # ``(mesocycle.order, index)`` tuple ``_scroll_hint`` walks. One list
        # serves the anchor pick and the chip strip.
        plan_weeks = list(
            Week.objects.filter(mesocycle__plan_id=plan.pk, deleted_at__isnull=True)
            .select_related("mesocycle")
            .order_by("mesocycle__order", "index")
        )
        if not plan_weeks:
            cards.append(
                {
                    "id": plan.pk,
                    "title": plan.title,
                    "goal": plan.goal,
                    "coach": plan.coach.display_name(),
                    "block": "",
                    "sessions": [],
                    "grid": None,
                    "awaiting": True,
                }
            )
            continue

        # ``requested_week`` (the ``?week=`` override) wins over the derived
        # scroll hint when it names a live week of THIS plan — matched by
        # ``plan_id`` rather than a second per-card query. A foreign/
        # other-plan week never matches here, so it's a no-op for every other
        # card.
        if requested_week is not None and requested_week.mesocycle.plan_id == plan.pk:
            anchor = requested_week
        else:
            anchor = _scroll_hint(plan_weeks, user)
        block = anchor.mesocycle
        focus = anchor

        # The anchored week's sessions are the tappable log rows. Live rows
        # only (soft delete, designer framework Phase 0): a day the coach
        # removed after delivering is gone from the athlete's home too, and a
        # removed exercise stops counting toward the row's "N exercises" chip
        # (``_athlete_session_row`` reads it via ``session.trainable_cells()``,
        # already live-filtered — P0 fixed-lineup cutover).
        session_objs = list(focus.sessions.filter(deleted_at__isnull=True))
        done = _done_session_ids([s.pk for s in session_objs], user)
        sessions = [_athlete_session_row(s, done=s.pk in done) for s in session_objs]

        # Week chips (issue #456 Finding 1) span the WHOLE PLAN, not just the
        # anchored block: a coach adding a NEW block must stay reachable by
        # tapping a chip — the athlete could never tap into it otherwise.
        week_chip_groups = _week_chip_groups(plan_weeks, focus)

        cards.append(
            {
                "id": plan.pk,
                "title": plan.title,
                "goal": plan.goal,
                "coach": plan.coach.display_name(),
                "block": block.name,
                "sessions": sessions,
                "grid": _athlete_block_grid(block, focus.pk),
                "week_chip_groups": week_chip_groups,
                "chip_count": len(plan_weeks),
                "awaiting": False,
            }
        )
    # The most-recently-engaged program leads the list — a coach's edit to a
    # DIFFERENT plan must not bump it (see ``_athlete_default_plan_id``); sort
    # is stable, so every other card keeps its ``-modified`` relative order.
    cards.sort(key=lambda c: c["id"] != default_plan_id)
    return cards


def _week_chip_groups(plan_weeks, focus):
    """Navigation chips for every live week of the PLAN, grouped by block.

    Issue #456 Finding 1: the chip strip must span every live mesocycle, not
    just the anchored one, or a newly added block is permanently
    unreachable. Grouping is by mesocycle (order preserved from
    ``plan_weeks``, already plan-wide ordered); a plan with more
    than one block gets each group labeled with its block's name
    (``mesocycle.name`` — the same string the card header shows as
    ``plan.block``) so the jump is legible, while a single-block plan gets one
    unlabeled group — today's flat "Wk N" row, unchanged. ``focused`` is
    which column this card is showing right now (the ``?week=`` override, or
    the derived scroll hint — ``_scroll_hint``) — the only position flag a
    chip carries; the app has no "current" week to also flag (docs/meso/
    remove-current-week-plan.md).
    """
    multi_block = len({w.mesocycle_id for w in plan_weeks}) > 1
    groups = []
    group_meso_id = None
    group = None
    for w in plan_weeks:
        if w.mesocycle_id != group_meso_id:
            group = {"label": w.mesocycle.name if multi_block else "", "chips": []}
            groups.append(group)
            group_meso_id = w.mesocycle_id
        group["chips"].append(
            {
                "id": w.pk,
                "index": w.index,
                "label": _week_label(w),
                "focused": w.pk == focus.pk,
            }
        )
    return groups


def _prescribed_set_count(prescription):
    """How many set rows a prescription's cell asks for, or 0.

    Text-first (Phase 2a): the count comes from parsing the freeform cell —
    "3 x 12" is a plain count of 3, but "AMRAP" or a packed circuit parses to
    none; the caller falls back to a default otherwise.
    """
    parsed = prescription.parsed() or {}
    sets = parsed.get("sets")
    return max(sets, 0) if isinstance(sets, int) else 0


def _set_rows(prescription, logged, *, default=3, cap=12, hard_cap=60):
    """Pre-filled set-input rows for one prescription (Phase 2 logger).

    ``logged`` maps ``(prescription_id, set_number)`` to the athlete's own
    ``LoggedSet``. The row count is the prescribed sets (capped, or ``default``
    when the cell is free-form), widened to show every set the athlete already
    logged so a reload never hides logged data — but ``hard_cap`` bounds the
    render unconditionally so a stray large ``set_number`` can never balloon the
    page (the log endpoint also rejects set numbers above its own ceiling).

    ``logged`` must already be scoped to ``source_line__isnull=True`` by the
    caller (``athlete_session``) — a parse-at-commit ``LoggedSet`` derived from
    a freeform sub-line (5a) renders itself as that sub-line's text, so
    admitting it here too would double-display the same performed data as a
    phantom structured input row (plan §6).
    """
    prescribed = _prescribed_set_count(prescription) or default
    logged_numbers = [n for (pid, n) in logged if pid == prescription.pk]
    count = max(min(prescribed, cap), max(logged_numbers, default=0), 1)
    count = min(count, hard_cap)
    rows = []
    for n in range(1, count + 1):
        s = logged.get((prescription.pk, n))
        rows.append(
            {
                "set_number": n,
                "reps": s.reps if s else "",
                "load": s.load if s else "",
                "rpe": s.rpe if s else "",
                "done": s is not None,
            }
        )
    return rows


def _target_label(prescription, lines=()):
    """The prescribed target shown above a logger's set rows.

    The coach's freeform cell text, verbatim (Phase 2a) — what they typed IS
    the target, e.g. "4 x 6, RPE 9, 225" — with any non-blank sub-lines
    folded in after it (`` · ``-joined) so the whole stack reads as one line.
    """
    parts = [prescription.text]
    parts.extend(line.text for line in lines)
    return _text_label("\n".join(p for p in parts if p and p.strip()))


def athlete_session(session, athlete):
    """One session as the athlete's interactive logger (Phase 2).

    ``session`` is already athlete-scoped by the view; this formats
    the prescribed grid into set-input rows, pre-filled from the athlete's own
    most-recent ``SessionLog``, and reports its done status. Cells are read via
    ``session.cells()`` (P0 fixed-lineup cutover), already live-filtered.
    """
    log = (
        SessionLog.objects.filter(session=session, athlete=athlete)
        .order_by("-created_at")
        .prefetch_related("sets__source_line")
        .first()
    )
    # No double-display (5a, plan §6): a freeform sub-line's text already
    # renders itself (``_sub_lines`` below), so a ``LoggedSet`` DERIVED from
    # that same text must not ALSO render as a structured input row.
    #
    # The test is literally whether the source line still SHOWS that text —
    # see ``models.parsed_set_is_hidden``, the single predicate this and the
    # logger's replace-delete both use so they cannot drift apart. Nothing is
    # mutated to make a reclaimed set reappear, which is what ``history.py``
    # requires ("undo must never touch ... athlete data") since a coach edit
    # is undoable.
    logged = (
        {
            (s.prescription_id, s.set_number): s
            for s in log.sets.all()
            if not parsed_set_is_hidden(s)
        }
        if log
        else {}
    )
    done = log is not None and log.status == SessionLog.Status.DONE
    week = session.week
    prescriptions = list(session.trainable_cells())
    # The rows' freeform sub-lines (Phase 2a) — folded into each target label
    # below so the athlete sees the whole stack (an RPE row, cues), not just
    # the prescription line.
    lines_by_slot = defaultdict(list)
    for line_cell in session.line_cells():
        lines_by_slot[line_cell.exercise_slot_id].append(line_cell)

    def _sub_lines(slot_id):
        # The row's editable tracking stack (Phase 4a): its line>=1 cells for
        # this week as ``[{line, text, warn}]``. Blank cells are dropped from
        # the display (a cleared sub-line is a blank cell, not a deleted row).
        # ``warn`` (5a, plan §8) is derived on read, not stored: re-classify
        # the cell's own text with ``parse_performed`` and flag it only when
        # that read is ``unresolved-set`` — text that *looks* like a fat-
        # fingered set attempt (``225 x``) but didn't resolve. Every other
        # classification (skip/swap/note/duration/set) is a successful parse
        # and never warns.
        return [
            {
                "line": line_cell.line,
                "text": line_cell.text,
                "warn": cell_should_warn(line_cell.text),
            }
            for line_cell in lines_by_slot.get(slot_id, ())
            if line_cell.text.strip()
        ]

    # The athlete's persisted, log-derived 1RM per lift (in this plan's unit) — the
    # %1RM logger seeds its suggested bar load from it (no manual estimate needed).
    one_rm_map = one_rm_values(athlete, prescriptions, week.mesocycle.plan.unit)
    return {
        "id": session.pk,
        "n": session.day_number,
        "name": session.name,
        "bias": session.bias,
        "status": "done" if done else "pending",
        "status_label": "Logged" if done else "To do",
        "block": week.mesocycle.name,
        "week": f"Wk {week.index}",
        "plan_title": week.mesocycle.plan.title,
        # The plan's load unit (kg/lb) — the %1RM logger turns a "75%" target into
        # a bar load in this unit (S2 Phase 2b).
        "unit": week.mesocycle.plan.unit,
        "notes": log.notes if log else "",
        "log_url": reverse("meso:athlete_log_session", kwargs={"pk": session.pk}),
        # Where the logger persists a manually-entered 1RM (Phase 2) — server-side
        # now, so it syncs across devices and the coach can see it.
        "one_rm_url": reverse("meso:athlete_set_one_rm", kwargs={"pk": session.pk}),
        "exercises": [
            {
                **serialize_prescription(p, lines_by_slot.get(p.exercise_slot_id, ())),
                # ``target`` folds LINE 0 only now — the sub-line stack is
                # editable (``sub_lines``), so folding it into the read-only
                # target too would double-display it (Phase 4a).
                "target": _target_label(p),
                # The editable per-week tracking stack the athlete writes to.
                "sub_lines": _sub_lines(p.exercise_slot_id),
                # The stored 1RM as a bare number string ("140"), or "" — the
                # client appends the unit and may layer a typed override on top.
                "one_rm": _one_rm_label(one_rm_map.get(p.pk)),
                # Whether that value is auto-derived from logs or manually entered
                # ("logged"/"manual"/""), so the logger seeds the input from a
                # manual value but treats a logged one as a placeholder.
                "one_rm_source": _one_rm_source(one_rm_map.get(p.pk)),
                "set_rows": _set_rows(p, logged),
            }
            for p in prescriptions
        ],
    }


def _one_rm_label(one_rm):
    """A stored ``AthleteOneRm`` as the bare number string the client reads, or ""."""
    return _fmt_num(one_rm.value) if one_rm is not None else ""


def _one_rm_source(one_rm):
    """A stored ``AthleteOneRm``'s ``source`` ("logged"/"manual"), or "" when none."""
    return one_rm.source if one_rm is not None else ""


def athlete_log_payload(session_ctx):
    """The JSON the Alpine logger hydrates from (and POSTs back).

    A trimmed view of ``athlete_session``: just what the client needs to render
    the set rows and submit them — the log URL, current status, and per-exercise
    rows. Kept separate from the display dict so the template's ``json_script``
    payload stays small and intentional.
    """
    return {
        "log_url": session_ctx["log_url"],
        # Where a manually-entered 1RM is persisted server-side (Phase 2).
        "one_rm_url": session_ctx["one_rm_url"],
        # Where the athlete's freeform sub-line cells are upserted (Phase 4a).
        "cell_url": reverse(
            "meso:athlete_cell_write", kwargs={"pk": session_ctx["id"]}
        ),
        "status": session_ctx["status"],
        # The unit lets the %1RM helper render a suggested bar load (S2 Phase 2b).
        "unit": session_ctx["unit"],
        "exercises": [
            {
                "id": e["id"],
                "name": e["name"],
                "target": e["target"],
                # The freeform cell text (Phase 2a) — the client recovers a
                # %1RM target from it (its "NN%" token) to offer the
                # estimated-1RM helper (`percentTarget` in meso_athlete.js).
                "text": e["text"],
                # The persisted 1RM ("140"/"") + its source — a manual value seeds
                # the input; a log-derived one is the suggested-load default shown
                # as a placeholder.
                "one_rm": e.get("one_rm", ""),
                "one_rm_source": e.get("one_rm_source", ""),
                "note": e.get("note", ""),
                "tag": e.get("tag", ""),
                # The editable tracking stack (Phase 4a) — the client hydrates a
                # freeform sub-line input per entry, saved on blur.
                "sub_lines": e.get("sub_lines", []),
                "set_rows": e["set_rows"],
            }
            for e in session_ctx["exercises"]
        ],
    }


def _pct_label(threshold):
    """A fraction (``Decimal("0.5")``) as a whole-percent string (``"50"``).

    ``normalize`` strips the trailing zeros a ``×100`` leaves; the ``:f`` format
    keeps it out of scientific notation (``5E+1`` → ``"50"``).
    """
    return f"{(threshold * 100).normalize():f}"


def usage_dashboard(report, *, threshold):
    """Adapt a usage :class:`Report` into the owner dashboard's template context.

    The owner-facing read surface (agent-usage Phase 4). Reuses the report's own
    objects (``coaches``/``totals`` carry their cost/revenue/margin properties) and
    adds: a ``YYYY-MM`` month label with prev/next links for navigation, the
    margin-alert subset (paying coaches over ``threshold`` × revenue), and the
    roll-ups pre-sorted by cost. ``threshold`` is the alert fraction (see
    ``agent_usage_report.resolve_alert_threshold``).
    """
    year, month = report.start.year, report.start.month
    prev_year, prev_month = agent_usage_report.shift_month(year, month, -1)
    next_year, next_month = agent_usage_report.shift_month(year, month, 1)
    return {
        "report": report,
        "month_label": report.start.strftime("%Y-%m"),
        "prev_month": f"{prev_year:04d}-{prev_month:02d}",
        "next_month": f"{next_year:04d}-{next_month:02d}",
        "threshold": threshold,
        "threshold_pct": _pct_label(threshold),
        "alerts": agent_usage_report.margin_alerts(report, threshold),
        "by_tier": agent_usage_report.sorted_totals(report.by_tier),
        "by_model": agent_usage_report.sorted_totals(report.by_model),
        "by_trigger": agent_usage_report.sorted_totals(report.by_trigger),
    }


def tour_funnel(*, variant=None, since=None):
    """Aggregate :class:`TourEvent` rows into the staff funnel dashboard's context.

    The read side of the guided-tour analytics (#441 P3-6): the ``record_*``
    helpers write one row per funnel moment; this rolls them up per-kind,
    per-variant, and per-advance-step, plus a compact Started → Opt-in →
    Completed funnel. Everything is ORM-aggregated (``values(...).annotate(
    Count(...))``) — no row ever loads into Python. Optional ``variant`` /
    ``since`` narrow the scope; the default is all-time, all-variants.

    Contract (the view + tests read these exact keys):

    - ``event_counts`` — every ``Kind`` 0-filled → count.
    - ``by_variant`` — ``{"sandbox": {...}, "self": {...}}``, both present, each
      kind 0-filled (so a variant with no events still renders a full row).
    - ``step_advances`` — ``[{"step_key", "count"}]`` for ADVANCED events, in the
      tour ``STEPS`` order. Only steps that actually appear are emitted (0-count
      steps are intentionally omitted — the table lists what happened, ordered
      canonically, not every possible step).
    - ``funnel`` — ordered display stages, each ``{"label", "count", "pct"}``.
      ``count`` is the raw event total for the stage (Started / Opt-in /
      Completed) so reaped null-coach sandbox rows are never dropped; ``pct`` is
      that count over Started, clamped to <= 100% (one sandbox tour emits several
      ``opt_in`` rows, so opt-in events can exceed starts).
    - ``total_events`` — all events in scope.
    """
    qs = TourEvent.objects.all()
    if variant is not None:
        qs = qs.filter(variant=variant)
    if since is not None:
        qs = qs.filter(created__gte=since)

    kinds = [value for value, _ in TourEvent.Kind.choices]
    variants = [value for value, _ in TourEvent.Variant.choices]

    event_counts = {kind: 0 for kind in kinds}
    for row in qs.values("kind").annotate(n=Count("id")):
        if row["kind"] in event_counts:
            event_counts[row["kind"]] = row["n"]

    by_variant = {v: {kind: 0 for kind in kinds} for v in variants}
    for row in qs.values("variant", "kind").annotate(n=Count("id")):
        bucket = by_variant.get(row["variant"])
        if bucket is not None and row["kind"] in bucket:
            bucket[row["kind"]] = row["n"]

    # ADVANCED counts per step, re-ordered into the canonical tour STEP order.
    advance_counts = {
        row["step_key"]: row["n"]
        for row in qs.filter(kind=TourEvent.Kind.ADVANCED)
        .values("step_key")
        .annotate(n=Count("id"))
    }
    step_order = [step["key"] for step in tour.STEPS]
    step_advances = [
        {"step_key": key, "count": advance_counts[key]}
        for key in step_order
        if key in advance_counts
    ]

    # Funnel = raw event counts per stage, NOT distinct coaches: the sandbox
    # expiry sweep reaps throwaway coaches to ``coach = NULL`` (SET_NULL), and a
    # distinct-coach count would silently drop all that historical sandbox
    # traffic. Raw counts keep every row. The tradeoff — one sandbox tour emits
    # several ``opt_in`` rows, so opt-in events can exceed starts — is handled by
    # clamping the displayed conversion to <= 100%.
    started = event_counts["started"]

    def _pct(n):
        return min(100, round(100 * n / started)) if started else 0

    funnel = [
        {"label": "Started", "count": started, "pct": 100 if started else 0},
        {
            "label": "Opt-in",
            "count": event_counts["opt_in"],
            "pct": _pct(event_counts["opt_in"]),
        },
        {
            "label": "Completed",
            "count": event_counts["completed"],
            "pct": _pct(event_counts["completed"]),
        },
    ]

    return {
        "event_counts": event_counts,
        "by_variant": by_variant,
        "step_advances": step_advances,
        "funnel": funnel,
        "total_events": qs.count(),
    }
