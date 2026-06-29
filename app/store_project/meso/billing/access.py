"""The one place that answers "can this coach do X?" (S6 billing, D10).

A thin read over ``CoachSubscription`` so a request gates **without calling
Stripe** (D8) — Stripe is the source of truth, this is the fast local mirror.
Two gates share one predicate (``is_active``): the **seat cap** (∞ active
athletes when active, else ``FREE_SEAT_LIMIT``) and the **AI agent** (paid-only
— the Claude agent has real per-call cost). A billable seat is an *active*
``CoachAthlete`` link (pending invites/requests don't count).

A coach with **no subscription row** gates exactly as ``free`` — existing
coaches predate billing, and a missing row should never crash or over-grant.

Phase 1 is these accessors + the model state only; **nothing is enforced yet**.
The invite/request choke points (``can_add_athlete``) and the agent endpoint
(``can_use_agent``) wire these in at Phase 3. See ``docs/meso/billing-plan.md``.
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
