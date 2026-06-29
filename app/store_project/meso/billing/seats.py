"""Best-effort inline seat-quantity sync at the relationship choke points (S6, Phase 2).

When a coach's active-athlete count changes — an invite is accepted, a relationship
ends — a *paid* coach's Stripe subscription quantity should follow so they're billed
for the right number of seats. That sync is **best-effort and must never break the
relationship change**: it's deferred to ``transaction.on_commit`` (so a rolled-back
request syncs nothing, matching the deliver-email / agent-job pattern) and any Stripe
error is swallowed + logged. The daily ``reconcile_seats`` qcluster sweep is the
correctness backstop that recomputes from truth.

The gate/enforcement (blocking a free coach at the cap) is Phase 3; this is only the
*sync* of an already-allowed change.
"""

import logging

from django.db import transaction

from . import stripe_gateway

logger = logging.getLogger(__name__)


def schedule_seat_sync(coach):
    """Defer a best-effort Stripe seat-quantity sync for ``coach`` to after commit."""

    def _run():
        try:
            stripe_gateway.sync_seat_quantity(coach)
        except Exception:  # noqa: BLE001 — best-effort; the daily sweep is the backstop
            logger.exception("Seat-quantity sync failed for coach %s", coach.pk)

    transaction.on_commit(_run)
