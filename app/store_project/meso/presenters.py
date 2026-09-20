"""Adapt real Meso models into the dict shapes the templates expect.

The roster/profile templates were built against a fixtures module (since
retired). Phase 1 feeds them real, scoped data for everything that exists yet —
the athlete, their training history, and their (global) contraindications.
Roster **compliance** (the per-athlete adherence meter) and **activity** (the
recent-completed-sessions feed) are now wired off real logged data via
``adherence``; the profile's ``compliance`` and ``has_program`` stay neutral
until those surfaces grow their own slices.
"""

import datetime
import math
import statistics
from collections import defaultdict

from django.db.models import CharField
from django.db.models import Count
from django.db.models import Exists
from django.db.models import Min
from django.db.models import OuterRef
from django.db.models import Prefetch
from django.db.models import Q
from django.db.models import Subquery
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import PushKind
from store_project.notifications.models import PushNotification
from store_project.notifications.models import SentEmail
from store_project.users.models import User

from . import adherence
from . import parsing
from . import tour
from .billing import access as billing_access
from .billing import agent_usage_report
from .models import MAX_LOGGED_SET_NUMBER
from .models import AgentProposalBatch
from .models import CoachAthlete
from .models import CoachInvite
from .models import CoachSubscription
from .models import LoggedSet
from .models import Mesocycle
from .models import Plan
from .models import PlanAction
from .models import PushSubscription
from .models import Session
from .models import SessionLog
from .models import TourEvent
from .models import Week
from .models import WeekDelivery
from .models import display_line_id
from .models import hidden_parsed_set_pks
from .models import line_displays
from .models import newest_session_logs
from .models import sub_line_warn_reason
from .one_rm import key_str
from .one_rm import one_rm_values
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


def billing_state(coach, *, checkout_pending=False):
    """The coach's billing/paywall state for the roster (S6 Phase 3).

    A template-friendly read over ``billing/access.py`` + the subscription row:
    the tier, seat usage, and which upgrade CTAs to offer. The free tier sees
    "start your no-card trial" (single-use) and "subscribe"; a coach with a real
    Stripe subscription sees "manage billing" (the hosted Portal); an over-limit
    coach (post-downgrade, D6) sees the freeze warning naming how many athletes are
    suspended (``suspended_count``, S6 Phase 5). ``seat_limit`` is ``None`` for an
    unlimited (active/trial/comped) coach so the template hides the cap.

    ``on_trial`` (#555) now means only the **local** no-card trial — a coach who
    subscribed mid-trial (a Stripe trial, ``is_stripe_trial``) has a real Stripe
    subscription and shows "Pro — first charge on X" instead, via
    ``first_charge_at``. ``deferred_first_charge`` is what Subscribing *right
    now* would defer to (``billing/access.deferred_first_charge``) — present
    only when there's enough of the local trial left for Stripe to accept it;
    the template falls back to an "under 2 days" charge-today notice otherwise.

    ``checkout_pending`` (#556, item 2) is the ``?billing=success`` query param
    a completed Checkout's ``success_url`` always carries — passed in by the
    view, since the webhook that would update the mirror may not have arrived
    yet. It's exposed here only when it's still *true*: once
    ``has_live_stripe_subscription`` goes true the mirror has caught up, so
    the flag turns itself off and the page shows the real state instead of a
    stale "finishing" placeholder.

    ``past_due`` (#556, item 1) is its own state — a past_due coach has a live
    (if unpaid) Stripe subscription, so it must never fall through to the
    free-plan Subscribe copy. ``cancelling``/``pro_until``/``is_stripe_trial``
    (#556, item 3) mirror a scheduled cancel (``CoachSubscription.cancel_at``):
    a live trialing/active row that's scheduled to end reads "Pro until
    {date}" instead of promising a first charge or a renewal that won't
    happen — ``past_due`` wins over ``cancelling`` (no Pro access to
    promise), and ``first_charge_at`` is suppressed while cancelling.
    ``cancelling`` also requires ``cancel_at`` to still be in the future
    (adversarial review of #556): a delayed or dropped
    ``customer.subscription.deleted`` webhook would otherwise leave "Pro
    until {a past date}" on the page forever even though the coach still
    reads as active — once that date has passed, this falls through to the
    ordinary active/first-charge branch instead.

    ``show_subscribe``/``show_manage_billing`` fold the scattered per-template
    conditions (comped, live, pending, over_limit, on_trial, …) into two keys
    so a template branches on one read instead of re-deriving the logic.
    """
    sub = getattr(coach, "coach_subscription", None)
    status = sub.status if sub else CoachSubscription.Status.FREE
    seat_limit = billing_access.effective_seat_limit(coach)
    active = billing_access.is_active(coach)
    live = bool(sub and sub.has_live_stripe_subscription)
    # Once the mirror shows a live subscription, the page shows the real
    # state — the pending placeholder is only for the gap before the webhook
    # lands. ``pending`` deliberately outranks ``over_limit`` below (via
    # ``show_subscribe``'s ``not pending`` gate) — a coach who just paid
    # shouldn't be told to re-subscribe while the webhook is still in
    # flight. It can never coexist with ``past_due``: ``past_due`` requires
    # ``live`` (a real, if unpaid, Stripe subscription), and ``pending`` is
    # ANDed with ``not live`` — so the two are mutually exclusive by
    # construction, not by template ordering. A ``comped`` row is excluded
    # too (#556 review, round 3): comped isn't a live Stripe subscription, so
    # a coach comped by an admin while a pending marker from their own
    # just-completed Checkout was still fresh would otherwise read
    # "Finishing your subscription…" over their real, unlimited plan.
    pending = (
        checkout_pending and not live and status != CoachSubscription.Status.COMPED
    )
    over_limit = billing_access.is_over_limit(coach)
    past_due = live and status == CoachSubscription.Status.PAST_DUE
    is_stripe_trial = bool(sub and sub.is_stripe_trial)
    # past_due wins: a past_due coach has no Pro access to promise, so a
    # scheduled cancel on top of that is moot.
    cancelling = bool(
        sub
        and live
        and sub.cancel_at is not None
        and sub.cancel_at > timezone.now()
        and status
        in (CoachSubscription.Status.TRIALING, CoachSubscription.Status.ACTIVE)
    )
    on_trial = (
        active
        and status == CoachSubscription.Status.TRIALING
        and not (sub and sub.stripe_subscription_id)
    )
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
        "has_live_stripe_subscription": live,
        "checkout_pending": pending,
        "past_due": past_due,
        "cancelling": cancelling,
        "is_stripe_trial": is_stripe_trial,
        # The date a scheduled cancel ends Pro access — always set whenever
        # ``cancelling`` is true (#556, item 3).
        "pro_until": sub.cancel_at if cancelling else None,
        "on_trial": on_trial,
        "trial_end": sub.trial_end if sub else None,
        # The Stripe-trial first-charge date (#555) — None off a Stripe trial
        # (a local trial, any other status), or while cancelling (never
        # promise a charge that won't happen).
        "first_charge_at": (
            sub.trial_end if sub and sub.is_stripe_trial and not cancelling else None
        ),
        # What subscribing *right now* would defer the first charge to, or None
        # when there isn't enough of the local trial left (#555).
        "deferred_first_charge": billing_access.deferred_first_charge(coach),
        "seat_count": billing_access.active_seat_count(coach),
        "seat_limit": None if seat_limit == math.inf else int(seat_limit),
        "can_add_athlete": billing_access.can_add_athlete(coach),
        "can_use_agent": billing_access.can_use_agent(coach),
        "agent": agent_allowance(coach),
        "over_limit": over_limit,
        # How many active athletes are soft-suspended by the downgrade (S6 Phase 5):
        # 0 unless over the limit, then the count beyond the oldest free cap.
        "suspended_count": len(billing_access.suspended_athlete_ids(coach)),
        "can_start_trial": can_start_trial,
        "has_stripe_subscription": bool(sub and sub.stripe_subscription_id),
        # The two keys the templates branch the Subscribe / Manage billing
        # forms on (#556): a comped or already-live (or pending) coach never
        # sees Subscribe; Manage billing shows for any coach with a real
        # subscription id, or while a Checkout is pending and a customer
        # already exists.
        "show_subscribe": (
            not live
            and not pending
            and status != CoachSubscription.Status.COMPED
            and (not active or on_trial or over_limit)
        ),
        "show_manage_billing": bool(sub and sub.stripe_subscription_id)
        or (pending and bool(coach.stripe_customer_id)),
        "price_summary": PRICE_SUMMARY,
    }


def coach_billing(coach, *, checkout_pending=False):
    """The coach-facing billing & usage page context (agent-usage — coach surface).

    The complement to the staff-only owner dashboard (``usage_dashboard``): that
    shows org-wide **cost** (COGS); this shows *one coach* their **bill** (the flat
    monthly Pro price they owe, D14) and **how much agent they've used** this month,
    broken down per athlete. The hard line: a coach sees what they pay and how
    much they've used, **never** the internal per-run cost estimate, so this context
    carries run counts and the flat price only — no ``cost``/``margin`` keys.

    The month window is the report's current calendar month, the same window the
    agent meter counts against, so ``runs_this_month`` reconciles with the allowance
    in ``state["agent"]``. ``checkout_pending`` is passed straight through to
    ``billing_state`` (#556, item 2).
    """
    state = billing_state(coach, checkout_pending=checkout_pending)
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


def _text_lines(text):
    """A freeform cell's text as its non-blank lines, stripped, in order."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _text_label(text):
    """A one-line display label for a freeform cell's text (Phase 2a).

    The coach's own notation IS the target label now — verbatim, with a
    multi-line cell folded to `` · `` separators; "—" for a blank cell.
    """
    return " · ".join(_text_lines(text)) or "—"


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


def _avg_rpe_delta(prescriptions, sets_by_slot, sub_lines_by_slot):
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
        for s in sets_by_slot.get(prescription.exercise_slot_id, []):
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
    scores its sets against the prescribed targets. "Most recent" means the
    newest log by ``created_at`` (models.newest_session_logs, #579) — not the
    athlete-supplied workout ``date`` — so this agrees with the athlete's own
    page (``athlete_session``) about which log is current. A pending draft
    (the athlete hit "Save progress" but hasn't finished) is not feedback
    yet, so it — like an unlogged session — renders an honest awaiting state
    (targets only, 0% complete) rather than inventing numbers. ``session``
    arrives coach-scoped; its cells are read via ``session.cells()`` (P0
    fixed-lineup cutover).
    """
    plan = session.week.mesocycle.plan
    athlete = plan.athlete
    prescriptions = list(session.trainable_cells())
    sub_lines_by_slot = _coach_sub_lines_by_slot(session)
    # #579: shares the newest-log rule every other "current SessionLog for
    # this (session, athlete) pair" read uses — see models.newest_session_logs.
    #
    # `Prefetch("sets", ...select_related("prescription"))` (#578 C1), not a
    # plain `"sets__prescription"` lookup: a set's `anchor_slot_id` falls back
    # to `prescription.exercise_slot_id` when `exercise_slot_id` is NULL (the
    # transitional dual-read), and `select_related` joins that in the same
    # query that fetches `sets` instead of costing a second query.
    log = (
        newest_session_logs(session, athlete, status=SessionLog.Status.DONE)
        .prefetch_related(
            Prefetch("sets", queryset=LoggedSet.objects.select_related("prescription"))
        )
        .first()
    )
    # Keyed by ANCHOR SLOT id, not `prescription_id` (#578 C1): safe because
    # ``unique_cell_slot_week_line`` makes ``(exercise_slot, week, line)``
    # unique, so within this one session's week each slot has at most one
    # line-0 cell — ``prescriptions`` (``session.trainable_cells()``) below
    # therefore has distinct ``exercise_slot_id``s, and grouping logged sets
    # by that id keys them AT LEAST AS COARSELY as the old `prescription_id`
    # did — never more finely, and strictly coarser for a NULL-`prescription`
    # row (#577/#581), which the old key dropped from every group entirely
    # and this one now folds in under its surviving `exercise_slot`. That's
    # not a free win: if the replacement row `_upsert_parsed_set` mints
    # beside such an orphan is ALSO live in this log, both key to the same
    # slot now, so `logged_n`
    # below double-counts that one performance, `completion`'s numerator
    # double-counts it too, and `_avg_rpe_delta` averages in the same RPE
    # twice. Same double-count `views.py`'s `_upsert_parsed_set` comment
    # documents and defers to C2 — not fixed here: deciding which of two real
    # rows is the duplicate is the model question #578 defers, not a grouping
    # change to make quietly in a presenter.
    sets_by_slot = defaultdict(list)
    if log is not None:
        for s in log.sets.all():
            slot_id = s.anchor_slot_id
            if slot_id is not None:
                sets_by_slot[slot_id].append(s)

    results = [
        _exercise_result(
            p, sets_by_slot.get(p.exercise_slot_id, []), plan.unit, sub_lines_by_slot
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
        logged_n = len(sets_by_slot.get(p.exercise_slot_id, []))
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
                prescriptions, sets_by_slot, sub_lines_by_slot
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


def _cell_lines(cell):
    """One athlete-table cell's prescription stack, one entry per line.

    Reads a ``serialize_mesocycle_grid`` cell dict (the athlete table is
    transformed from that coach grid, not from ``Prescription`` rows): the
    freeform ``text`` verbatim (Phase 2a), then any non-blank sub-lines, so
    the athlete sees the whole stack. The phone's stacked view (issue #508)
    renders these as separate lines; ``_cell_summary`` folds them into one.
    """
    parts = [cell["text"]]
    parts.extend(line["text"] for line in cell.get("lines", ()))
    return _text_lines("\n".join(p for p in parts if p and p.strip()))


def _cell_summary(cell):
    """A read-only prescription summary for one athlete-table cell.

    ``_cell_lines`` folded to one `` · ``-separated string ("—" when blank),
    for the wide table's single-line cells.
    """
    return " · ".join(_cell_lines(cell)) or "—"


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

    The phone's stacked view (issue #508) shows that focused column alone:
    ``focus_week`` names it, and each row's ``focus`` is its cell there —
    ``lines`` one entry per line, ``off`` when the row isn't trained that
    week (skipped, or no cell).
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
            focus = {"lines": [], "off": True}
            for w, key in zip(columns, col_keys):
                cell = row["cells"].get(key)
                focused = w["id"] == focus_week_id
                if cell is None:
                    cells.append({"present": False, "focused": focused})
                    continue
                if not cell["skipped"]:
                    has_trainable = True
                if focused:
                    focus = {"lines": _cell_lines(cell), "off": cell["skipped"]}
                cells.append(
                    {
                        "present": True,
                        "focused": focused,
                        "summary": _cell_summary(cell),
                        "skipped": cell["skipped"],
                    }
                )
            if has_trainable:
                rows.append({"name": row["name"], "cells": cells, "focus": focus})
        if rows:
            days.append(
                {
                    "name": day["name"],
                    "bias": day["bias"],
                    "day_number": day["day_number"],
                    "rows": rows,
                }
            )
    focus_week = next((w for w in weeks if w["focused"]), None)
    return {"weeks": weeks, "days": days, "focus_week": focus_week}


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


def _set_rows(
    prescription, logged, *, default=3, cap=12, hard_cap=MAX_LOGGED_SET_NUMBER
):
    """Pre-filled set-input rows for one prescription (Phase 2 logger).

    ``logged`` maps ``(prescription_id, set_number)`` to the athlete's own
    ``LoggedSet``. The row count is the prescribed sets (capped, or ``default``
    when the cell is free-form), widened to show every set the athlete already
    logged so a reload never hides logged data — but ``hard_cap`` bounds the
    render unconditionally so a stray large ``set_number`` can never balloon the
    page. #570: ``hard_cap`` defaults to the SAME ``MAX_LOGGED_SET_NUMBER`` the
    log endpoint enforces (both the posted-set_number ceiling and the
    collision-renumbering walk's cap) — a mismatch here is exactly what let a
    row past 50 render as an ordinary fillable row the endpoint would then
    reject outright.

    ``logged`` must already be scoped to ``source_line__isnull=True`` by the
    caller (``athlete_session``) — a parse-at-commit ``LoggedSet`` derived from
    a freeform sub-line (5a) renders itself as that sub-line's text, so
    admitting it here too would double-display the same performed data as a
    phantom structured input row (plan §6).

    Each row also carries ``id`` — the ``LoggedSet.pk`` visible at this
    (prescription, set_number), or ``None`` for a blank/unlogged row (#567,
    row identity). This is how the client learns the id it should post back
    for a row it's editing, instead of the server having to infer which row a
    save means from ``(prescription, set_number)`` alone — evidence that goes
    stale the moment a hidden row's own number moves out from under it.
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
                "id": s.pk if s else None,
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
    ``session.trainable_cells()`` (P0 fixed-lineup cutover) — live AND
    non-skipped, so a skipped row is never presented to the athlete as
    loggable. (The docstring said ``session.cells()``; the code has used
    ``trainable_cells()`` since, and the difference matters: it is why a skipped
    row needs no warn handling on reload, only in the cell-write response.)
    """
    # #567/#568/#579: the shared newest-log rule — see models.newest_session_logs.
    # Without it, this read and the blur response's
    # (``views._cell_warn_reason_or_blank``) could each pick a different "newest"
    # log for a tied pair and disagree about what backs a line.
    log = (
        newest_session_logs(session, athlete)
        .prefetch_related("sets__source_line", "sets__reclaimed_line")
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
    #
    # #561: a coach undo restores a reclaimed line's text without touching
    # ``LoggedSet``, so the row it now shows can be a source-less copy
    # answering only through ``reclaimed_line``. Computed once over the whole
    # log (``hidden_parsed_set_pks``) rather than per row, because that ranking
    # — a line shows at most one performance — can only be answered by looking
    # at every row that could be displayed by the same line, not one at a time.
    all_sets = list(log.sets.all()) if log else []
    hidden_pks = hidden_parsed_set_pks(all_sets)
    logged = (
        {
            (s.prescription_id, s.set_number): s
            for s in all_sets
            if s.pk not in hidden_pks
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

    # Sub-lines whose text is currently backed by a parsed set — i.e. the row
    # exists AND still matches what the line says. Reuses the same predicate the
    # suppression rule uses, so "displayed by its line" means one thing here.
    #
    # Keyed on ``display_line_id`` (#561), not the raw ``source_line_id``: a
    # copy left behind by "Log session" answers to its line only through
    # ``reclaimed_line``, and a cell's ``backing_sets`` has to include it or a
    # line a coach undo restored gets tinted as unlogged despite the set the
    # copy carries.
    sets_by_line = {}
    for row in all_sets:
        sets_by_line.setdefault(display_line_id(row), []).append(row)

    # The same rows, for the days this exercise is NOT on any more (#572). A
    # coach's cross-day drag re-points the `ExerciseSlot`, so the sub-line cell
    # travels to the new day while the `LoggedSet` it derived stays on the old
    # day's log — #568's decision, and it means the line reads as unlogged here.
    # The tint is right; a re-post is not, because the performance already
    # exists. `_lineNeedsSending` re-posts a warned line whose text hasn't
    # changed, so without a reason to tell this case apart from the un-skip case
    # it exists for, merely focusing and leaving such a line minted a SECOND row
    # for one performance. One query for the whole session, grouped the same way
    # `sets_by_line` is, rather than one per warned line.
    #
    # Restricted to the cells that could actually REACH the branch that reads
    # it. `sub_line_warn_reason` consults `elsewhere_sets` only when the text
    # resolves to a set AND nothing on THIS day is already showing it, so both
    # tests are repeated here: a cue, a note, a blank cell or a skip/swap
    # can never get that far, and neither can a line already backed by its own
    # row — which is every line of a session the athlete is actively logging.
    # Without the second test this query still fired (and still found nothing
    # any line could use) on exactly the sessions that see the most traffic.
    # Both predicates are the same calls on the same in-memory objects the
    # reason itself will make, so narrowing here cannot change an answer.
    line_cell_pks = [
        cell.pk
        for cells in lines_by_slot.values()
        for cell in cells
        if parsing.performed_is_set(cell.text)
        and line_displays(cell, sets_by_line.get(cell.pk, ())) is None
    ]
    elsewhere_by_line = defaultdict(list)
    if line_cell_pks:
        for row in (
            LoggedSet.objects.filter(
                Q(source_line_id__in=line_cell_pks)
                | Q(source_line__isnull=True, reclaimed_line_id__in=line_cell_pks),
                session_log__athlete=athlete,
            ).exclude(session_log__session_id=session.pk)
            # No `select_related` (F2): every row here only ever reaches
            # `line_displays` (via `sub_line_warn_reason`), which reads
            # `source_line_id`/`reclaimed_line_id` and re-parses the CELL's
            # own text — never a related `source_line`/`reclaimed_line`
            # object. Joining them in was pure waste.
        ):
            elsewhere_by_line[display_line_id(row)].append(row)

    def _sub_lines(slot_id):
        # The row's editable tracking stack (Phase 4a): its line>=1 cells for
        # this week as ``[{line, text, warn}]``. Blank cells are dropped from
        # the display (a cleared sub-line is a blank cell, not a deleted row).
        # ``warn`` (5a, plan §8) is derived on read, not stored: re-classify
        # the cell's own text with ``parse_performed``. Text that *looks* like a
        # fat-fingered set attempt (``225 x``) warns; skip/swap/note/duration
        # are successful parses and never do.
        #
        # A line whose text DOES resolve to a set also warns when no such row
        # exists. Asking after the row rather than re-deriving "is this row
        # loggable right now" catches every reason one can be missing — the
        # coach had skipped the line when it was typed and later unskipped it,
        # the values were too long to store, the tolerance guard swallowed a
        # database error — and each of those leaves the same state this warning
        # exists for: ordinary-looking performed text that quietly counts for
        # nothing.
        # #567/#568 P2-C: no ``loggable`` is passed here, so it defaults
        # ``True`` — hard-wired, unlike ``views._cell_warn_reason_or_blank``, which
        # passes ``not skipped``. The two answers still agree, but only
        # because of an INVISIBLE coupling at the call site, not because a
        # skipped line can't warn: ``_sub_lines`` is only ever called (below)
        # for ``p in prescriptions``, and ``prescriptions`` comes from
        # ``session.trainable_cells()`` (this function's own docstring, above,
        # already records that this exact distinction from ``session.cells()``
        # drifted once), which excludes every skipped cell before ``_sub_lines``
        # ever runs. If a future change ever rendered a skipped row's sub-lines
        # here too — a "show its history" mode, say — this default would warn
        # a line that cannot accept a set at all, exactly the disagreement
        # #568 exists to prevent; that caller would need to pass its own
        # ``loggable=not skipped`` rather than relying on this default.
        rendered = []
        for line_cell in lines_by_slot.get(slot_id, ()):
            if not line_cell.text.strip():
                continue
            # #572: the client needs WHY, not just whether — see
            # `sub_line_warn_reason`. `warn` stays a bool so every template
            # and the client's own tinting are untouched; `warn_reason` rides
            # alongside it, and an older client that ignores the new key
            # simply behaves as it does today.
            reason = sub_line_warn_reason(
                line_cell,
                backing_sets=sets_by_line.get(line_cell.pk, ()),
                elsewhere_sets=elsewhere_by_line.get(line_cell.pk, ()),
            )
            rendered.append(
                {
                    "line": line_cell.line,
                    "text": line_cell.text,
                    "warn": reason is not None,
                    "warn_reason": reason or "",
                }
            )
        return rendered

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


# ---------------------------------------------------------------------------
# Product analytics (#509 slice 2): the staff dashboard at /meso/analytics/.
#
# ``track()`` (``analytics/track.py``) writes one ``Event`` row per first-party
# action; this rolls those up alongside the pre-existing Meso tables (``Plan``/
# ``PlanAction``/``WeekDelivery``/``AgentProposalBatch``/``CoachInvite``/
# ``CoachAthlete``/``SessionLog``/``PushSubscription``/``CoachSubscription``)
# into one owner-facing read of active users, the invite→delivery→log
# activation funnel, feature adoption, and Meso's own transactional email.
# Everything here is ORM aggregation — ``values()``/``annotate()``/
# ``aggregate()``, correlated ``Subquery``/``OuterRef``, ``pk__in=<queryset>``
# — so the view runs a fixed number of queries regardless of how much data
# exists. The only rows ever loaded into Python are the two funnel cohorts
# (bounded by the window; needed for ``statistics.median`` since SQLite has no
# percentile function). See ``docs/meso/decisions.md`` "First-party usage
# events (#509)" for the product rationale.
# ---------------------------------------------------------------------------

#: Event names that count as "the coach did something" (C6 below).
COACH_EVENT_NAMES = (
    EventName.PLAN_CREATED,
    EventName.TEMPLATE_IMPORTED,
    EventName.AGENT_PROPOSAL_RUN,
    EventName.BATCH_APPLIED,
    EventName.BLOCK_DELIVERED,
    EventName.INVITE_SENT,
)

#: ``SentEmail``/``EmailEvent`` kinds the Email section reports on, in display
#: order — the four Meso-authored kinds. Account/marketing/store transactional
#: kinds live on the SES deliverability dashboard instead.
MESO_EMAIL_KINDS = (
    EmailKind.BLOCK_DELIVERED,
    EmailKind.COACH_INVITE,
    EmailKind.INVITE_REMINDER,
    EmailKind.COACH_REQUEST,
)

#: ``PushNotification`` kinds the Push table reports on, in display order
#: (#509 slice 3). Only ``BLOCK_DELIVERED`` is ever sent today —
#: ``PushKind.OTHER`` exists purely as a model-level safety default for a
#: sender that forgets to name itself — but the table stays keyed off this
#: tuple, the same shape as ``MESO_EMAIL_KINDS``, so a second push kind only
#: needs a new entry here.
MESO_PUSH_KINDS = (PushKind.BLOCK_DELIVERED,)


def _ineligible_users():
    """Users excluded everywhere below: staff, or a throwaway sandbox account.

    Mirrors ``analytics.track.track()``'s own exclusion exactly, so a coach
    invisible to the event ledger is invisible here too. Left unordered
    (``.order_by()``) since it's only ever used as an ``__in=`` subquery.
    """
    return User.objects.filter(
        Q(is_staff=True) | Q(sandbox_session__isnull=False)
    ).order_by()


def _subject_ids(queryset):
    """String pks of ``queryset``'s rows, for matching ``Event.subject_id``.

    ``subject_id`` is a plain string column — non-numeric subject ids exist
    elsewhere, so it's never cast *from*. This casts the known-integer pk side
    instead, so ``Event.objects.filter(subject_type=..., subject_id__in=
    _subject_ids(qs))`` can compare it against an AutoField-keyed table.
    """
    return (
        queryset.order_by()
        .annotate(sid=Cast("pk", output_field=CharField()))
        .values("sid")
    )


def _demo_event_exclusion():
    """Q excluding ``Event`` rows about a demo relationship's plan.

    Coach events about a plan carry ``props.demo`` from the moment they're
    written, which is what survives "Remove demo data": that deletes the demo
    plans, and a subject match finds nothing afterwards. The subject match
    covers events written before the prop existed while their demo rows are
    still there. ``Event`` has no FK to a plan — subjects are opaque
    ``(subject_type, subject_id)`` strings — so it names every subject type demo
    coach activity shows up as: the plan itself, one of its mesocycles
    (blocks), or one of its agent runs.
    """
    demo_plans = Plan.objects.filter(relationship__is_demo=True)
    demo_mesocycles = Mesocycle.objects.filter(plan__relationship__is_demo=True)
    demo_batches = AgentProposalBatch.objects.filter(plan__relationship__is_demo=True)
    return (
        # ``has_key`` first: an event without the key reads the JSON value as
        # SQL NULL, and ``NOT (NULL OR …)`` would exclude it.
        Q(props__has_key="demo", props__demo=True)
        | Q(subject_type="meso.plan", subject_id__in=_subject_ids(demo_plans))
        | Q(
            subject_type="meso.mesocycle",
            subject_id__in=_subject_ids(demo_mesocycles),
        )
        | Q(
            subject_type="meso.agentproposalbatch",
            subject_id__in=_subject_ids(demo_batches),
        )
    )


def _client_athletes():
    """Users who are (or were) coached by someone else: the athlete side's population.

    An athlete-side ``Event`` or device row can't always be traced back to the
    plan it was about — clearing a typed line deletes the log a ``set_logged``
    event points at — so athlete counts also require the actor to have a link
    that is neither self-coaching nor demo. A coach who only trains on their
    own program is never an athlete here, whatever was deleted since.
    """
    return (
        CoachAthlete.objects.filter(
            is_self=False,
            is_demo=False,
            # Coached at some point: an active link, or one that was active
            # and ended. A pending or declined request isn't coaching.
            status__in=(CoachAthlete.Status.ACTIVE, CoachAthlete.Status.ENDED),
        )
        .order_by()
        .values("athlete_id")
    )


def _self_subject_exclusion():
    """Q excluding athlete ``Event`` rows about a self-coaching plan's session or log."""
    self_sessionlog_ids = _subject_ids(
        SessionLog.objects.filter(
            session__week__mesocycle__plan__relationship__is_self=True
        )
    )
    self_session_ids = _subject_ids(
        Session.objects.filter(week__mesocycle__plan__relationship__is_self=True)
    )
    return Q(subject_type="meso.sessionlog", subject_id__in=self_sessionlog_ids) | Q(
        subject_type="meso.session", subject_id__in=self_session_ids
    )


def product_analytics(*, days, now=None):
    """Aggregate Meso's usage tables into the staff dashboard's context (#509).

    ``now`` defaults to ``timezone.now()``. ``days`` sets the report's own
    window (``[since, now]``, inclusive both ends) for the funnel, feature
    adoption, and email sections; active-user WAU/MAU are always the trailing
    7/30 days regardless of ``days`` (``window`` uses ``days``). Every count
    excludes ``_ineligible_users()`` (staff and sandbox accounts) and, for
    coach-side rows, demo-relationship activity (``CoachAthlete.is_demo``,
    ``_demo_event_exclusion``). Self-coaching (``is_self``) is real coach
    activity and stays in; athlete-side rows count only
    ``_client_athletes()`` and leave self-coaching sessions out, since that
    isn't a client training.

    Contract (the view + template read these exact keys):

    - ``days`` / ``since`` / ``now`` — the resolved window.
    - ``events_since`` — the earliest ``Event`` ever recorded (``None`` before
      the first one), so the template can caveat every Event-sourced number as
      only meaningful since that date (the #509 slice-1 deploy).
    - ``active_users`` — ``{"coaches": {...}, "athletes": {...}}``, each
      ``{"wau", "mau", "window"}`` distinct-user counts.
    - ``funnel`` — the three activation-funnel rows (``email_invite``,
      ``athlete_request``, ``all``), in that order.
    - ``features`` — the fourteen feature-adoption rows, in the fixed order
      the dashboard displays them.
    - ``email`` — ``{"rows": [...], "totals": {...}}`` for the Meso-authored
      ``SentEmail`` kinds.
    - ``push`` — ``{"rows": [...], "totals": {...}}`` for ``PushNotification``,
      shown as a second table inside the Email card (#509 slice 3).
    """
    now = now or timezone.now()
    since = now - datetime.timedelta(days=days)
    wau_since = now - datetime.timedelta(days=7)
    mau_since = now - datetime.timedelta(days=30)

    events_since = Event.objects.aggregate(m=Min("created"))["m"]

    return {
        "days": days,
        "since": since,
        "now": now,
        "events_since": events_since,
        "active_users": {
            "coaches": _active_role_counts(
                _coach_activity_sources,
                wau_since=wau_since,
                mau_since=mau_since,
                since=since,
                now=now,
            ),
            "athletes": _active_role_counts(
                _athlete_activity_sources,
                wau_since=wau_since,
                mau_since=mau_since,
                since=since,
                now=now,
            ),
        },
        "funnel": _activation_funnel(since=since, until=now),
        "features": _feature_adoption(since=since, until=now),
        "email": _email_section(since=since, until=now),
        "push": _push_section(since=since, until=now),
    }


# -- 1. active users --------------------------------------------------------


def _active_role_counts(source_builder, *, wau_since, mau_since, since, now):
    """One distinct-user ``.count()`` per window, for one role (coach/athlete).

    ``source_builder(lower, upper)`` returns the list of user-id subqueries
    for that role's activity sources in ``[lower, upper]`` (C1-C6 or A1-A2);
    this ORs them together and counts distinct, ineligible-excluded users —
    three queries total (wau/mau/window), fixed regardless of data size.
    """
    ineligible = _ineligible_users()

    def _count(lower):
        sources = source_builder(lower, now)
        q = Q(pk__in=sources[0])
        for source in sources[1:]:
            q |= Q(pk__in=source)
        return User.objects.exclude(pk__in=ineligible).filter(q).count()

    return {
        "wau": _count(wau_since),
        "mau": _count(mau_since),
        "window": _count(since),
    }


def _coach_activity_sources(since, until):
    """User-id subqueries for every active-coach source in ``[since, until]`` (C1-C6)."""
    plan_actions = PlanAction.objects.filter(
        created_at__gte=since, created_at__lte=until
    )
    plans_created = Plan.objects.filter(created__gte=since, created__lte=until)
    deliveries = WeekDelivery.objects.filter(
        delivered_at__gte=since, delivered_at__lte=until
    ).exclude(week__mesocycle__plan__relationship__is_demo=True)
    agent_batches = (
        AgentProposalBatch.objects.filter(created_at__gte=since, created_at__lte=until)
        .exclude(trigger=AgentProposalBatch.Trigger.EVAL)
        .exclude(plan__relationship__is_demo=True)
    )
    invites = CoachInvite.objects.filter(created_at__gte=since, created_at__lte=until)
    coach_events = Event.objects.filter(
        name__in=COACH_EVENT_NAMES, created__gte=since, created__lte=until
    ).exclude(_demo_event_exclusion())

    return [
        # C1 — PlanAction: relationship coach (not demo) + template owner.
        plan_actions.filter(plan__relationship__is_demo=False)
        .order_by()
        .values_list("plan__relationship__coach_id", flat=True),
        plan_actions.filter(plan__is_template=True)
        .order_by()
        .values_list("plan__owner_id", flat=True),
        # C2 — Plan.created: the same relationship/template split.
        plans_created.filter(relationship__is_demo=False)
        .order_by()
        .values_list("relationship__coach_id", flat=True),
        plans_created.filter(is_template=True)
        .order_by()
        .values_list("owner_id", flat=True),
        # C3 — WeekDelivery (demo already excluded above).
        deliveries.order_by().values_list(
            "week__mesocycle__plan__relationship__coach_id", flat=True
        ),
        # C4 — AgentProposalBatch (eval trigger + demo already excluded above).
        agent_batches.order_by().values_list("coach_id", flat=True),
        # C5 — CoachInvite.
        invites.order_by().values_list("coach_id", flat=True),
        # C6 — coach-named Event (demo subjects already excluded above).
        coach_events.order_by().values_list("actor_id", flat=True),
    ]


def _athlete_activity_sources(since, until):
    """User-id subqueries for every active-athlete source in ``[since, until]`` (A1-A2)."""
    logged_sets = (
        SessionLog.objects.filter(
            created_at__gte=since, created_at__lte=until, sets__isnull=False
        )
        .exclude(session__week__mesocycle__plan__relationship__is_demo=True)
        .exclude(session__week__mesocycle__plan__relationship__is_self=True)
        .order_by()
        .values_list("athlete_id", flat=True)
    )
    athlete_events = (
        Event.objects.filter(
            name__in=(EventName.SET_LOGGED, EventName.SESSION_OPENED),
            created__gte=since,
            created__lte=until,
            actor__in=_client_athletes(),
        )
        .exclude(_self_subject_exclusion())
        .order_by()
        .values_list("actor_id", flat=True)
    )
    return [
        logged_sets,  # A1
        athlete_events,  # A2
    ]


# -- 2. activation funnel ----------------------------------------------------


def _activation_funnel(*, since, until):
    """The three funnel rows: ``email_invite``, ``athlete_request``, ``all`` (#509 §2)."""
    email_rows = _email_invite_cohort(since, until)
    request_rows = _athlete_request_cohort(since, until)
    return [
        _funnel_row("email_invite", "Email invite", email_rows),
        _funnel_row("athlete_request", "Athlete request", request_rows),
        _funnel_row("all", "All", email_rows + request_rows),
    ]


def _email_invite_cohort(since, until):
    """Raw cohort rows for the ``email_invite`` path — one query, window-bounded.

    ``t_delivered``/``t_logged`` are correlated subqueries (``Subquery`` +
    ``OuterRef``), the second referencing the first's own annotation, so
    nothing beyond the cohort itself (invites *sent* in the window) ever loads
    into Python.
    """
    ineligible = _ineligible_users()
    delivered_sq = (
        WeekDelivery.objects.filter(
            week__mesocycle__plan__relationship_id=OuterRef("accepted_link_id"),
            delivered_at__gte=OuterRef("responded_at"),
        )
        .order_by("delivered_at")
        .values("delivered_at")[:1]
    )
    logged_sq = (
        SessionLog.objects.filter(
            athlete_id=OuterRef("accepted_by_id"),
            session__week__mesocycle__plan__relationship_id=OuterRef(
                "accepted_link_id"
            ),
            sets__isnull=False,
            created_at__gte=OuterRef("t_delivered"),
        )
        .order_by("created_at")
        .values("created_at")[:1]
    )
    raw = (
        CoachInvite.objects.filter(created_at__gte=since, created_at__lte=until)
        .exclude(coach__in=ineligible)
        .exclude(accepted_by__in=ineligible)
        .annotate(t_delivered=Subquery(delivered_sq))
        .annotate(t_logged=Subquery(logged_sq))
        .values("created_at", "status", "responded_at", "t_delivered", "t_logged")
    )
    rows = []
    for row in raw:
        t_accepted = (
            row["responded_at"]
            if row["status"] == CoachInvite.Status.ACCEPTED
            else None
        )
        t_delivered = row["t_delivered"] if t_accepted is not None else None
        t_logged = row["t_logged"] if t_delivered is not None else None
        rows.append(
            {
                "t_sent": row["created_at"],
                "t_accepted": t_accepted,
                "t_delivered": t_delivered,
                "t_logged": t_logged,
            }
        )
    return rows


def _athlete_request_cohort(since, until):
    """Raw cohort rows for the ``athlete_request`` path (#509 §2) — one query.

    Excludes a link that an email invite in this same cohort claimed — an
    athlete's pending request the coach's invite happened to also accept — so
    it's never counted under both paths. A link claimed by an invite sent
    before the window stays here: that invite isn't in the email cohort, and
    dropping the request too would count it nowhere.
    """
    ineligible = _ineligible_users()
    claimed_link_ids = CoachInvite.objects.filter(
        created_at__gte=since, created_at__lte=until, accepted_link__isnull=False
    ).values("accepted_link_id")
    delivered_sq = (
        WeekDelivery.objects.filter(
            week__mesocycle__plan__relationship_id=OuterRef("pk"),
            delivered_at__gte=OuterRef("responded_at"),
        )
        .order_by("delivered_at")
        .values("delivered_at")[:1]
    )
    logged_sq = (
        SessionLog.objects.filter(
            athlete_id=OuterRef("athlete_id"),
            session__week__mesocycle__plan__relationship_id=OuterRef("pk"),
            sets__isnull=False,
            created_at__gte=OuterRef("t_delivered"),
        )
        .order_by("created_at")
        .values("created_at")[:1]
    )
    raw = (
        CoachAthlete.objects.filter(
            created_at__gte=since,
            created_at__lte=until,
            invited_by=CoachAthlete.InvitedBy.ATHLETE,
            is_demo=False,
            is_self=False,
        )
        .exclude(pk__in=claimed_link_ids)
        .exclude(coach__in=ineligible)
        .exclude(athlete__in=ineligible)
        .annotate(t_delivered=Subquery(delivered_sq))
        .annotate(t_logged=Subquery(logged_sq))
        .values("created_at", "status", "responded_at", "t_delivered", "t_logged")
    )
    accepted_statuses = (CoachAthlete.Status.ACTIVE, CoachAthlete.Status.ENDED)
    rows = []
    for row in raw:
        t_accepted = row["responded_at"] if row["status"] in accepted_statuses else None
        t_delivered = row["t_delivered"] if t_accepted is not None else None
        t_logged = row["t_logged"] if t_delivered is not None else None
        rows.append(
            {
                "t_sent": row["created_at"],
                "t_accepted": t_accepted,
                "t_delivered": t_delivered,
                "t_logged": t_logged,
            }
        )
    return rows


def _funnel_row(key, label, rows):
    """One funnel row's counts + step medians from a path's normalized rows."""
    accepted = [r for r in rows if r["t_accepted"] is not None]
    delivered = [r for r in accepted if r["t_delivered"] is not None]
    logged = [r for r in delivered if r["t_logged"] is not None]
    return {
        "key": key,
        "label": label,
        "sent": len(rows),
        "accepted": len(accepted),
        "delivered": len(delivered),
        "logged": len(logged),
        "median_to_accept": _median_delta(
            r["t_accepted"] - r["t_sent"] for r in accepted
        ),
        "median_to_deliver": _median_delta(
            r["t_delivered"] - r["t_accepted"] for r in delivered
        ),
        "median_to_log": _median_delta(
            r["t_logged"] - r["t_delivered"] for r in logged
        ),
    }


def _median_delta(deltas):
    """``statistics.median`` over an iterable of ``timedelta``, or ``None`` if empty."""
    deltas = list(deltas)
    return statistics.median(deltas) if deltas else None


# -- 3. feature adoption ------------------------------------------------------


def _coach_event_stats(name, *, since, until, extra=None):
    """``{"users", "times"}`` for a coach-side ``Event``-sourced feature row.

    Excludes ineligible actors and demo-subject events, same as C6.
    """
    qs = Event.objects.filter(name=name, created__gte=since, created__lte=until)
    qs = qs.exclude(actor__in=_ineligible_users()).exclude(_demo_event_exclusion())
    if extra:
        qs = qs.filter(**extra)
    return qs.aggregate(users=Count("actor", distinct=True), times=Count("pk"))


def _athlete_event_stats(name, *, since, until):
    """``{"users", "times"}`` for an athlete-side ``Event``-sourced feature row.

    Same athlete rule as A2: the actor is someone else's client, and the event
    isn't about a self-coaching plan's session or log. A NULL actor (a deleted
    account) stays in ``times``, as everywhere on the page.
    """
    qs = (
        Event.objects.filter(name=name, created__gte=since, created__lte=until)
        .filter(Q(actor__isnull=True) | Q(actor__in=_client_athletes()))
        .exclude(actor__in=_ineligible_users())
        .exclude(_self_subject_exclusion())
    )
    return qs.aggregate(users=Count("actor", distinct=True), times=Count("pk"))


def _client_event_stats(name, *, since, until, props=None):
    """``{"users", "times"}`` for a browser-beacon feature row (#509 slice 3).

    ``pwa_installed`` and ``push_permission`` arrive through the client
    beacon (``Event.source == "client"``) with no subject at all — there's no
    plan or session to trace back to, so unlike ``_athlete_event_stats`` this
    applies neither the client-athlete filter nor the self-coaching subject
    exclusion. The beacon fires from the athlete-facing PWA surface (the
    install banner, the push-permission prompt), which a self-coaching coach
    training themselves also sees — there's no principled way to say that
    visit "isn't a client," and nothing on the row to check it against even
    if there were. That's why these rows are labelled "anyone" rather than
    "athletes" in ``_feature_adoption``. ``props`` optionally narrows to a
    JSON prop (e.g. ``{"props__result": "granted"}``) — a plain equality
    match, not ``has_key``: an event with no such prop reads that key as SQL
    NULL, and ``NULL = 'granted'`` is never true, so it's correctly left out
    without an explicit existence check (memory note ``meso-509-dashboard``'s
    "JSON has_key NULL trap" is about ``NOT (...)`` compositions, which this
    isn't).
    """
    qs = Event.objects.filter(name=name, created__gte=since, created__lte=until)
    qs = qs.exclude(actor__in=_ineligible_users())
    if props:
        qs = qs.filter(**props)
    return qs.aggregate(users=Count("actor", distinct=True), times=Count("pk"))


def _agent_batch_stats(trigger, *, since, until):
    """``{"users", "times"}`` for the ``agent_draft``/``agent_run`` rows."""
    qs = (
        AgentProposalBatch.objects.filter(
            trigger=trigger, created_at__gte=since, created_at__lte=until
        )
        .exclude(coach__in=_ineligible_users())
        .exclude(plan__relationship__is_demo=True)
    )
    return qs.aggregate(users=Count("coach", distinct=True), times=Count("pk"))


def _block_delivered_stats(*, since, until):
    """``users`` = distinct coaches; ``times`` = distinct (block, delivered_at) pairs.

    One block delivery writes a ``WeekDelivery`` per week, all stamped with
    the same ``delivered_at`` — counting rows would overcount a multi-week
    block as several "times".
    """
    qs = (
        WeekDelivery.objects.filter(delivered_at__gte=since, delivered_at__lte=until)
        .exclude(week__mesocycle__plan__relationship__coach__in=_ineligible_users())
        .exclude(week__mesocycle__plan__relationship__is_demo=True)
    )
    users = qs.aggregate(
        n=Count("week__mesocycle__plan__relationship__coach", distinct=True)
    )["n"]
    times = qs.values("week__mesocycle_id", "delivered_at").distinct().count()
    return {"users": users, "times": times}


def _invite_sent_stats(*, since, until):
    """``{"users", "times"}`` for the ``invite_sent`` row."""
    qs = CoachInvite.objects.filter(
        created_at__gte=since, created_at__lte=until
    ).exclude(coach__in=_ineligible_users())
    return qs.aggregate(users=Count("coach", distinct=True), times=Count("pk"))


def _trial_started_stats(*, since, until):
    """Trials that *started* in the window — ``trial_end - TRIAL_DAYS`` falls in it."""
    lead = datetime.timedelta(days=CoachSubscription.TRIAL_DAYS)
    qs = CoachSubscription.objects.filter(
        trial_end__gte=since + lead, trial_end__lte=until + lead
    ).exclude(coach__in=_ineligible_users())
    return qs.aggregate(users=Count("coach", distinct=True), times=Count("pk"))


def _push_enabled_stats(*, since, until):
    """``{"users", "times"}`` for the ``push_enabled`` row (client athletes only)."""
    qs = PushSubscription.objects.filter(
        created_at__gte=since,
        created_at__lte=until,
        athlete__in=_client_athletes(),
    ).exclude(athlete__in=_ineligible_users())
    return qs.aggregate(users=Count("athlete", distinct=True), times=Count("pk"))


def _feature_row(key, label, who, source, stats):
    return {"key": key, "label": label, "who": who, "source": source, **stats}


def _feature_adoption(*, since, until):
    """The fourteen feature-adoption rows (#509 §3), in display order.

    Each row is its own independent ``.aggregate()``/``.count()`` call — a
    fixed number of queries, never one per row of underlying data.
    """
    return [
        _feature_row(
            "plan_created",
            "New program",
            "coaches",
            "Event plan_created",
            _coach_event_stats(EventName.PLAN_CREATED, since=since, until=until),
        ),
        _feature_row(
            "agent_draft",
            "Draft with AI",
            "coaches",
            "AgentProposalBatch (draft)",
            _agent_batch_stats(
                AgentProposalBatch.Trigger.DRAFT, since=since, until=until
            ),
        ),
        _feature_row(
            "agent_run",
            "Agent run",
            "coaches",
            "AgentProposalBatch (manual)",
            _agent_batch_stats(
                AgentProposalBatch.Trigger.MANUAL, since=since, until=until
            ),
        ),
        _feature_row(
            "batch_applied",
            "Agent changes applied",
            "coaches",
            "Event batch_applied",
            _coach_event_stats(EventName.BATCH_APPLIED, since=since, until=until),
        ),
        _feature_row(
            "template_imported",
            "Template imported",
            "coaches",
            "Event template_imported",
            _coach_event_stats(EventName.TEMPLATE_IMPORTED, since=since, until=until),
        ),
        _feature_row(
            "block_delivered",
            "Block delivered",
            "coaches",
            "WeekDelivery",
            _block_delivered_stats(since=since, until=until),
        ),
        _feature_row(
            "invite_sent",
            "Invite sent",
            "coaches",
            "CoachInvite",
            _invite_sent_stats(since=since, until=until),
        ),
        _feature_row(
            "trial_started",
            "Trial started",
            "coaches",
            "CoachSubscription.trial_end",
            _trial_started_stats(since=since, until=until),
        ),
        _feature_row(
            "subscription_started",
            "Pro subscription started",
            "coaches",
            "Event subscription_started (stripe)",
            _coach_event_stats(
                EventName.SUBSCRIPTION_STARTED,
                since=since,
                until=until,
                extra={"props__via": "stripe"},
            ),
        ),
        _feature_row(
            "subscription_cancelled",
            "Subscription cancelled",
            "coaches",
            "Event subscription_cancelled",
            _coach_event_stats(
                EventName.SUBSCRIPTION_CANCELLED, since=since, until=until
            ),
        ),
        _feature_row(
            "push_enabled",
            "Push notifications enabled",
            "athletes",
            "PushSubscription",
            _push_enabled_stats(since=since, until=until),
        ),
        _feature_row(
            "pwa_installed",
            "App installed (PWA)",
            "anyone",
            "Event pwa_installed",
            _client_event_stats(EventName.PWA_INSTALLED, since=since, until=until),
        ),
        _feature_row(
            "push_permission_granted",
            "Push permission granted",
            "anyone",
            "Event push_permission",
            _client_event_stats(
                EventName.PUSH_PERMISSION,
                since=since,
                until=until,
                props={"props__result": "granted"},
            ),
        ),
        _feature_row(
            "session_completed",
            "Session completed",
            "athletes",
            "Event session_completed",
            _athlete_event_stats(EventName.SESSION_COMPLETED, since=since, until=until),
        ),
    ]


# -- 4. email -----------------------------------------------------------------


def _email_row(kind, label, counts):
    """One email row's rate math, shared between a per-kind row and ``totals``."""
    sent = counts["sent"]
    row = {
        "sent": sent,
        "delivered": counts["delivered"],
        "opened": counts["opened"],
        "clicked": counts["clicked"],
        "open_rate": _rate(counts["opened"], sent),
        "click_rate": _rate(counts["clicked"], sent),
    }
    if kind is not None:
        row = {"kind": kind, "label": label, **row}
    return row


def _rate(n, sent):
    """A percentage of ``sent``, rounded and capped at 100; ``None`` at 0 sent."""
    return min(100, round(100 * n / sent)) if sent else None


def _email_section(*, since, until):
    """The Email section: per-kind + totals, Meso-authored kinds only (#509 §4).

    One query: ``Count(..., filter=Q(...), distinct=True)`` per event type
    counts *distinct messages* with at least one such event, not raw event
    rows (two opens on one message is one "opened" message).
    """
    kind_labels = dict(EmailKind.choices)
    qs = SentEmail.objects.filter(
        kind__in=MESO_EMAIL_KINDS, sent_at__gte=since, sent_at__lte=until
    ).exclude(user__in=_ineligible_users())
    counts_by_kind = {
        row["kind"]: row
        for row in qs.values("kind").annotate(
            sent=Count("pk", distinct=True),
            delivered=Count(
                "pk",
                filter=Q(events__event_type=EmailEvent.EventType.DELIVERY),
                distinct=True,
            ),
            opened=Count(
                "pk",
                filter=Q(events__event_type=EmailEvent.EventType.OPEN),
                distinct=True,
            ),
            clicked=Count(
                "pk",
                filter=Q(events__event_type=EmailEvent.EventType.CLICK),
                distinct=True,
            ),
        )
    }
    empty = {"sent": 0, "delivered": 0, "opened": 0, "clicked": 0}
    rows = [
        _email_row(kind, kind_labels[kind], counts_by_kind.get(kind, empty))
        for kind in MESO_EMAIL_KINDS
    ]
    totals = _email_row(
        None,
        None,
        {
            key: sum(row[key] for row in rows)
            for key in ("sent", "delivered", "opened", "clicked")
        },
    )
    return {"rows": rows, "totals": totals}


# -- 5. push (Email card, second table) ---------------------------------------


def _push_row(kind, label, counts):
    """One push row's rate math, shared between a per-kind row and ``totals``.

    Mirrors ``_email_row``'s shape (#509 slice 3): a push has no "delivered"
    signal the way SES gives email one — the send call to the push service
    either raised or didn't — so this reports ``failed`` in that slot instead.

    ``click_rate`` divides by **attempts** (``sent + failed``), not ``sent``
    alone (adversarial review of #509, fix 2). ``sent``/``failed`` partition
    the cohort by whether *our own send call* raised — but a push can still
    reach the athlete and get tapped after our side already logged it as
    failed (a ``PUSH_TIMEOUT_SECONDS`` timeout on our end, a delivery on
    theirs): that produces ``sent=0, failed=1, clicked=1``, and dividing by
    ``sent`` gives ``None`` (or, with a differently-shaped denominator, a rate
    clamped to a nonsensical 100%+) for a push we know for a fact was tapped.
    ``clicked`` stays counted over the *whole* cohort — a click is the
    strongest possible evidence a push arrived, whatever our own send call
    reported — while attempts (never zero when ``clicked`` is non-zero) gives
    a stable, monotonic denominator: "of the pushes we attempted, this share
    were tapped."
    """
    sent = counts["sent"]
    failed = counts["failed"]
    attempts = sent + failed
    row = {
        "sent": sent,
        "failed": failed,
        "clicked": counts["clicked"],
        "click_rate": _rate(counts["clicked"], attempts),
    }
    if kind is not None:
        row = {"kind": kind, "label": label, **row}
    return row


def _push_section(*, since, until):
    """The Push table inside the Email card: per-kind + totals (#509 slice 3).

    Cohort = ``PushNotification`` rows *sent* (``sent_at``) in the window,
    ineligible recipients excluded — the same window and exclusion shape
    ``_email_section`` uses, so the two tables read as one story about the
    same block-delivery nudge. Scoped to ``MESO_PUSH_KINDS`` in the query
    itself, the way ``_email_section`` scopes to ``MESO_EMAIL_KINDS``
    (adversarial review of #509, fix 1): a row with an out-of-scope kind
    (``PushKind.OTHER`` — the model's deliberate default for "a sender that
    forgot to name itself") must count nowhere on this page, so the exclusion
    is made explicit by construction rather than relying on the display loop
    below (``for kind in MESO_PUSH_KINDS``) to happen to leave it out.
    ``error`` partitions the cohort in two with no third state: blank means
    the push left for the service (``sent``), a non-empty string means the
    service rejected it (``failed``) — unlike ``_email_section``'s
    ``events__event_type`` filters, these are plain columns on the row
    itself, not a join to a related event table, so there's nothing here
    that can fan out and no ``distinct=True`` needed. ``clicked`` is
    ``clicked_at`` set — recorded once, on the athlete's own landing GET
    (``notifications.push.record_push_click``), never from the service
    worker. One ``.values("kind").annotate(...)`` query over the fixed
    ``MESO_PUSH_KINDS`` tuple, zero-filled for a kind with no rows in the
    window, the same as the email rows.
    """
    kind_labels = dict(PushKind.choices)
    qs = PushNotification.objects.filter(
        kind__in=MESO_PUSH_KINDS, sent_at__gte=since, sent_at__lte=until
    ).exclude(user__in=_ineligible_users())
    counts_by_kind = {
        row["kind"]: row
        for row in qs.values("kind").annotate(
            sent=Count("pk", filter=Q(error="")),
            failed=Count("pk", filter=~Q(error="")),
            clicked=Count("pk", filter=Q(clicked_at__isnull=False)),
        )
    }
    empty = {"sent": 0, "failed": 0, "clicked": 0}
    rows = [
        _push_row(kind, kind_labels[kind], counts_by_kind.get(kind, empty))
        for kind in MESO_PUSH_KINDS
    ]
    totals = _push_row(
        None,
        None,
        {key: sum(row[key] for row in rows) for key in ("sent", "failed", "clicked")},
    )
    return {"rows": rows, "totals": totals}
