"""The one place that answers "can this coach do X?" (S6 billing, D10).

A thin read over ``CoachSubscription`` so a request gates **without calling
Stripe** (D8) — Stripe is the source of truth, this is the fast local mirror.
Two gates share one predicate (``is_active``): the **seat cap** (∞ active
athletes when active, else ``FREE_SEAT_LIMIT``) and the **AI agent** (paid-only
— the Claude agent has real per-call cost). A billable seat is an *active*
``CoachAthlete`` link (pending invites/requests don't count).

A coach with **no subscription row** gates exactly as ``free`` — existing
coaches predate billing, and a missing row should never crash or over-grant.

Phase 3 wires these gates into the choke points: ``can_add_athlete`` at the
invite/request endpoints (block a free coach past the cap), ``can_use_agent`` at
the agent endpoint (402 for the free tier), and ``can_edit`` at the edit/deliver
endpoints (the D6 over-limit freeze). See ``docs/meso/billing-plan.md``.
"""

import math

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


def can_use_agent(coach):
    """Agent gate — only an active coach may run the paid AI program agent (D4)."""
    return is_active(coach)


def active_seat_count(coach):
    """Billable seats in use — the coach's *active* ``CoachAthlete`` links."""
    return CoachAthlete.objects.for_coach(coach).active().count()


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
    """Edit/deliver gate (D6) — may this coach mutate or deliver programs?

    Everyone can edit/deliver *except* a coach who is over their seat limit after a
    downgrade (``is_over_limit``); they keep read access but can't change or deliver
    a program until they re-subscribe or end relationships to get back within the
    free cap. Never deletes data — only freezes writes.
    """
    return not is_over_limit(coach)
