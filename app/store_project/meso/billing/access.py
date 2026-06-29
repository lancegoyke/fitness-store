"""The one place that answers "can this coach do X?" (S6 billing, D10).

A thin read over ``CoachSubscription`` so a request gates **without calling
Stripe** (D8) — Stripe is the source of truth, this is the fast local mirror.
The **seat cap** (∞ active athletes when active, else ``FREE_SEAT_LIMIT``) keys
off one predicate (``is_active``). The **AI agent** gate is the metered refinement
(S6 Phase 5): an active/comped coach is unlimited, while a free coach gets
``FREE_AGENT_ALLOWANCE`` runs per calendar month (the Claude agent has real
per-call cost, so the free tier is a taste, not a binary no). A billable seat is
an *active* ``CoachAthlete`` link (pending invites/requests don't count); an agent
run is an ``AgentProposalBatch`` (the batch table is the ledger — no separate
counter).

A coach with **no subscription row** gates exactly as ``free`` — existing
coaches predate billing, and a missing row should never crash or over-grant.

Phase 3 wired these gates into the choke points: ``can_add_athlete`` at the
invite/request endpoints (block a free coach past the cap), ``can_use_agent`` at
the agent endpoint (402 for the free tier), and ``can_edit`` at the edit/deliver
endpoints (the D6 over-limit freeze). Phase 5 meters ``can_use_agent`` (free-tier
allowance) and refines the edit freeze **per athlete** (``can_edit_plan`` /
``suspended_athlete_ids``): an over-limit coach keeps editing their oldest
``FREE_SEAT_LIMIT`` athletes and is frozen only on the rest. See
``docs/meso/billing-plan.md``.
"""

import math

from django.utils import timezone

from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription


def _subscription(coach):
    """The coach's subscription row, or None if they have none.

    The reverse one-to-one accessor raises an ``AttributeError`` subclass when
    there's no row, so ``getattr(..., None)`` cleanly yields None (treated as the
    free tier everywhere below).
    """
    return getattr(coach, "coach_subscription", None)


def is_active(coach):
    """The single predicate: does this coach have full (unlimited) access right now?

    True for an active/comped subscription or a live (un-lapsed) trial; False for
    free, past_due, canceled, a lapsed trial, or no subscription row at all.
    """
    sub = _subscription(coach)
    return bool(sub and sub.is_active)


def _current_period_start():
    """Midnight on the first of the current month — the free agent meter resets here.

    A calendar-month window (cheap to compute, obvious to a coach reading "this
    month") rather than a rolling 30-day window, which would need a per-coach
    anchor. ``timezone.now()`` is tz-aware, so the truncated value is too.
    """
    return timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def agent_runs_this_month(coach):
    """Agent runs this coach has started in the current calendar month.

    A run is an ``AgentProposalBatch`` — the endpoint creates exactly one per
    dispatched run (after the gate and the API-key check), so the batch table *is*
    the ledger; there's no separate counter to drift. Group runs are rejected
    before a batch exists, so they never count against the allowance.
    """
    return AgentProposalBatch.objects.filter(
        coach=coach, created_at__gte=_current_period_start()
    ).count()


def free_agent_runs_remaining(coach):
    """Agent runs left this month — ``math.inf`` when unlimited, else the remainder.

    An active/trial/comped coach is unlimited (``math.inf``); a free coach gets
    ``FREE_AGENT_ALLOWANCE`` minus what they've used this month, floored at 0. The
    active short-circuit means a paying coach never runs the count query.
    """
    if is_active(coach):
        return math.inf
    used = agent_runs_this_month(coach)
    return max(0, CoachSubscription.FREE_AGENT_ALLOWANCE - used)


def can_use_agent(coach):
    """Agent gate — may this coach run the AI program agent right now (D4)?

    An active/trial/comped coach always; a free coach while they still have runs
    left in their monthly allowance (S6 Phase 5 — the metered refinement of the old
    binary free=no-agent gate). Defended at the endpoint, not just the UI, because
    the per-call API cost is real.
    """
    return free_agent_runs_remaining(coach) > 0


def active_seat_count(coach):
    """Billable seats in use — the coach's *active*, non-demo ``CoachAthlete`` links.

    Demo athletes (the one-click first-run demo, ``meso/demo.py``) are active links
    on the roster but not paid seats, so loading the demo never trips the paywall.
    """
    return CoachAthlete.objects.for_coach(coach).billable().count()


def effective_seat_limit(coach):
    """Active-athlete cap: unlimited (``math.inf``) when active, else the free cap."""
    if is_active(coach):
        return math.inf
    return CoachSubscription.FREE_SEAT_LIMIT


def can_add_athlete(coach):
    """Seat gate — may this coach take on another active athlete (D4)?

    A free coach is capped at ``FREE_SEAT_LIMIT``; an active/trial/comped coach is
    unlimited (a paid coach is never *blocked* — they just pay for the seat).
    """
    return active_seat_count(coach) < effective_seat_limit(coach)


def is_over_limit(coach):
    """Is this coach holding more active athletes than their tier allows (D6)?

    The downgrade-landing state: an active/trial/comped coach is never over (their
    limit is unbounded), so this only ever fires for a free/lapsed coach who held
    paid seats and then dropped to free (Stripe cancel/lapse, or comp removal). The
    seat gate prevents *reaching* this state through the app, so it's reachable
    only by a downgrade — and while over the limit the coach is blocked from
    editing and delivering until back within the cap or re-subscribed.
    """
    return not is_active(coach) and active_seat_count(coach) > effective_seat_limit(
        coach
    )


def can_edit(coach):
    """Coarse edit/deliver gate (D6) — may this coach mutate or deliver *anything*?

    Everyone can edit/deliver *except* a coach who is over their seat limit after a
    downgrade (``is_over_limit``); they keep read access but can't change or deliver
    a program until they re-subscribe or end relationships to get back within the
    free cap. Never deletes data — only freezes writes. This is the coach-wide
    predicate; ``can_edit_plan`` is the per-athlete refinement (S6 Phase 5) and the
    fallback for a group plan, which has no single relationship to keep live.
    """
    return not is_over_limit(coach)


def suspended_athlete_ids(coach):
    """Active ``CoachAthlete`` ids a downgrade has soft-suspended (D6, S6 Phase 5).

    When a coach drops below their active-athlete count (``is_over_limit``), the app
    keeps their **oldest** ``FREE_SEAT_LIMIT`` active relationships live and freezes
    the rest: those plans go read-only for the coach (no edit/deliver) until they
    re-subscribe or end relationships to get back within the cap. Nothing is deleted.
    Keeping the *oldest* avoids the app arbitrarily picking which athletes to
    freeze — the longest-standing relationships stay editable; the most recently
    added (likely the ones that pushed the coach onto a paid plan) are the ones that
    suspend on a lapse.

    An active/comped coach (or one within the cap) is never over the limit, so this
    is an empty set for them — cheap, since ``is_over_limit`` short-circuits before
    any per-link query.
    """
    if not is_over_limit(coach):
        return frozenset()
    active_ids = list(
        CoachAthlete.objects.for_coach(coach)
        .billable()
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
    )
    # Keep the oldest FREE_SEAT_LIMIT live; everything after the cutoff is frozen.
    return frozenset(active_ids[CoachSubscription.FREE_SEAT_LIMIT :])


def can_edit_plan(plan):
    """Per-plan edit/deliver gate (D6) — may this plan be mutated or delivered?

    The per-athlete refinement of ``can_edit`` (S6 Phase 5): an **individual** plan
    is frozen only when *its own* relationship is soft-suspended, so an over-limit
    coach keeps full control of their oldest ``FREE_SEAT_LIMIT`` athletes' plans and
    is blocked only on the suspended ones. A **group** plan serves many athletes
    through no single relationship, so it falls back to the coarse coach-wide freeze
    (``can_edit``). Defended at the mutating endpoints, not just the UI.
    """
    if plan.relationship_id is None:  # a group plan — no single link to keep live
        return can_edit(plan.coach)
    return plan.relationship_id not in suspended_athlete_ids(plan.coach)
