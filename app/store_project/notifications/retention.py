"""Retention sweep for the ``PushNotification`` ledger (#509 slice 3).

The push peer of ``analytics.retention.purge_expired_events``: same batched
loop, same 13-month window, over ``PushNotification.sent_at`` instead of
``Event.created``. ``RETENTION`` is imported from ``analytics.retention``
rather than redefined here so the two windows can't drift apart — a future
change to one would otherwise silently leave the other stale.
"""

import logging

from django.utils import timezone

from store_project.analytics.retention import RETENTION

from .models import PushNotification

logger = logging.getLogger(__name__)


def purge_expired_push_notifications(*, now=None, batch_size=1000):
    """Delete every ``PushNotification`` older than ``RETENTION``; returns the count.

    Batched for the same reason ``purge_expired_events`` is: a single
    unbounded ``DELETE`` would hold its lock for as long as the whole sweep
    takes. Each batch is its own statement, deliberately not wrapped in
    ``transaction.atomic`` — a crash mid-sweep just leaves the remaining
    expired rows for the next run.
    """
    cutoff = (now or timezone.now()) - RETENTION
    deleted = 0
    while True:
        pks = list(
            PushNotification.objects.filter(sent_at__lt=cutoff).values_list(
                "pk", flat=True
            )[:batch_size]
        )
        if not pks:
            break
        # Count what the DELETE removed, not what the SELECT saw: an
        # overlapping run may have already taken some of these rows.
        deleted += PushNotification.objects.filter(pk__in=pks).delete()[0]
        if len(pks) < batch_size:
            break
    logger.info("Purged %d expired push notification(s).", deleted)
    return deleted
