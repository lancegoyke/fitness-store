"""Reconcile each paid coach's Stripe seat quantity with their active-athlete count.

The inline best-effort sync (``billing.seats.schedule_seat_sync``) keeps Stripe
current on the happy path, but a sync can miss (Stripe blip, a relationship change
outside the wired choke points). This daily sweep is the **correctness backstop**:
for every coach with a live Stripe subscription, recompute their active-seat count
and push it to Stripe if it drifted from the cached ``quantity`` (S6 billing,
Phase 2).

Idempotent and side-effect-free beyond the Stripe quantity update; one coach's
Stripe error never stops the sweep. Runs daily on the ``qcluster`` (registered as
a ``django_q.Schedule`` row, like the invite sweeps).

    manage.py meso_reconcile_seats
"""

import logging

from django.core.management.base import BaseCommand

from store_project.meso.billing import stripe_gateway
from store_project.meso.models import CoachSubscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Correct any drift between each paid coach's Stripe seat quantity and their active-athlete count."

    def handle(self, *args, **options):
        # Only coaches who have actually subscribed (a live Stripe subscription).
        # A canceled row keeps its (now-dead) ids for history but must not be
        # modified in Stripe; free / local-trial / comped coaches have no Stripe
        # subscription at all.
        paid = (
            CoachSubscription.objects.exclude(stripe_subscription_id="")
            .filter(
                status__in=(
                    CoachSubscription.Status.ACTIVE,
                    CoachSubscription.Status.PAST_DUE,
                )
            )
            .select_related("coach")
        )
        checked = 0
        reconciled = 0
        for sub in paid:
            checked += 1
            try:
                if stripe_gateway.sync_seat_quantity(sub.coach):
                    reconciled += 1
            except Exception:  # noqa: BLE001 — one coach's failure can't stop the sweep
                logger.exception(
                    "reconcile_seats: Stripe sync failed for coach %s", sub.coach_id
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"reconcile_seats: checked {checked}, corrected {reconciled}."
            )
        )
