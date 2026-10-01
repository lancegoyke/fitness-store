"""Activate waiting acceptances when a coach's plan gains seats (#649).

An athlete who accepts while the coach has no seat lands on
``CoachAthlete.Status.ACCEPTED_WAITING``. Every billing state change persists
through ``CoachSubscription.save()`` (local trial, the Stripe webhook's
``update_or_create``, ``comp``), so a ``post_save`` receiver is the one hook:
no new webhook path and no Stripe writes.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from store_project.meso.billing import access as billing_access
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription

logger = logging.getLogger(__name__)


def activate_waiting(coach_id):
    """Flip the coach's waiting links to active, oldest acceptance first.

    Up to the coach's remaining capacity (``effective_seat_limit`` minus active
    seats; unlimited activates them all). Returns the links activated. Lock order
    (decisions.md, "Row-lock order"): the coach ``User``, then the waiting
    ``CoachAthlete`` rows by ascending pk (both no-key), then the capacity is
    re-read inside the lock. Never takes an athlete's ``User`` lock.
    """
    with transaction.atomic():
        coach = (
            get_user_model()
            .objects.select_for_update(no_key=True)
            .filter(pk=coach_id)
            .first()
        )
        if coach is None:
            return []
        waiting = list(
            CoachAthlete.objects.select_for_update(no_key=True)
            .filter(coach_id=coach_id, status=CoachAthlete.Status.ACCEPTED_WAITING)
            .order_by("pk")
        )
        capacity = billing_access.effective_seat_limit(
            coach
        ) - billing_access.active_seat_count(coach)
        activated = []
        for link in sorted(
            waiting, key=lambda x: (x.responded_at or x.created_at, x.pk)
        ):
            if capacity <= 0:
                break
            link.status = CoachAthlete.Status.ACTIVE
            link.save(update_fields=["status"])
            activated.append(link)
            capacity -= 1
        return activated


def _activate_after_commit(coach_id):
    # A billing webhook (or the trial POST) must never fail because of this.
    try:
        with transaction.atomic():
            activate_waiting(coach_id)
    except Exception:
        logger.exception("Failed to activate waiting athletes for coach %s", coach_id)


@receiver(post_save, sender=CoachSubscription)
def activate_waiting_on_upgrade(sender, instance, **kwargs):
    """Schedule waiting-link activation once the saved state grants seats."""
    if not instance.is_active:
        return
    coach_id = instance.coach_id
    if not (
        CoachAthlete.objects.filter(
            coach_id=coach_id, status=CoachAthlete.Status.ACCEPTED_WAITING
        ).exists()
    ):
        return
    transaction.on_commit(lambda: _activate_after_commit(coach_id))
