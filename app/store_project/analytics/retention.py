"""13-month retention sweep for the first-party ``Event`` ledger (#509).

Events are write-once and never rolled up, so the table only grows; this caps
it at just over a year of history — long enough for month-over-month and
year-over-year product-analytics comparisons, short enough that the ledger
never becomes an unbounded liability. See ``docs/meso/decisions.md``
"First-party usage events (#509)".
"""

import logging

from dateutil.relativedelta import relativedelta
from django.utils import timezone

from .models import Event

logger = logging.getLogger(__name__)

#: How long an ``Event`` row survives before the sweep deletes it.
RETENTION = relativedelta(months=13)


def purge_expired_events(*, now=None, batch_size=1000):
    """Delete every ``Event`` older than ``RETENTION``; returns the count deleted.

    Batched rather than one big ``DELETE`` — a table this write-heavy can hold
    a very long tail once retention first ships, and a single unbounded
    statement would hold its lock for however long that takes. Each batch is
    its own statement (deliberately **not** wrapped in ``transaction.atomic``):
    a crash mid-sweep just leaves the remaining expired rows for the next run,
    rather than holding one long-lived transaction against a table this size.
    """
    cutoff = (now or timezone.now()) - RETENTION
    deleted = 0
    while True:
        pks = list(
            Event.objects.filter(created__lt=cutoff).values_list("pk", flat=True)[
                :batch_size
            ]
        )
        if not pks:
            break
        # Count what the DELETE removed, not what the SELECT saw: an overlapping
        # run (the scheduled one and a manual one) may have taken some already.
        deleted += Event.objects.filter(pk__in=pks).delete()[0]
        if len(pks) < batch_size:
            break
    logger.info("Purged %d expired analytics event(s).", deleted)
    return deleted
