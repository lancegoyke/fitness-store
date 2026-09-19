"""``track()``: record one first-party usage event (#509).

See ``track`` for the contract. The event names live in ``events.py``.
"""

import logging

from django.conf import settings
from django.db import transaction

from store_project.meso.sandbox import is_sandbox

from .events import EventName
from .models import Event

logger = logging.getLogger(__name__)


def track(name, actor=None, subject=None, **props):
    """Record one server-side event. One insert; never raises; returns nothing.

    ``name`` must be an ``EventName``. An unknown name raises ``ValueError``
    when ``settings.ANALYTICS_STRICT_EVENT_NAMES`` is on (tests, and local dev
    with ``DEBUG``) and is logged and dropped in production. Every call site
    has a test that drives it, so under test a typo fails at the line that
    made it; in production a typo that slipped through costs one event, never
    the coach's delivery or the athlete's set.

    **Excluded at the helper:** an actor who is a sandbox coach
    (``meso.sandbox.is_sandbox``) or staff (``is_staff``) is dropped silently,
    so demo traffic and our own clicking don't count. ``meso.TourEvent``
    doesn't do this; its funnel is mostly sandbox traffic by design.

    ``subject`` is any saved model instance, stored as its ``app_label.model``
    and ``str(pk)``. ``props`` must be JSON-serializable (UUIDs, dates and
    Decimals are, via ``DjangoJSONEncoder``).

    The insert runs in its own savepoint, inside a try/except. Callers run
    inside open transactions, and on PostgreSQL a failed statement aborts the
    whole transaction: catching the error isn't enough, the caller's own
    write would still roll back. The savepoint confines a failure to the
    event (see ``tests/test_track_postgres.py``).
    """
    if name not in EventName.values:
        if getattr(settings, "ANALYTICS_STRICT_EVENT_NAMES", False):
            raise ValueError(f"Unknown analytics event name: {name!r}")
        logger.error("analytics: dropped an event with an unknown name %r", name)
        return
    if actor is not None and not actor.is_authenticated:
        actor = None
    try:
        with transaction.atomic():
            if actor is not None and (actor.is_staff or is_sandbox(actor)):
                return
            Event.objects.create(
                name=str(name),
                actor=actor,
                subject_type=subject._meta.label_lower if subject is not None else "",
                subject_id=str(subject.pk) if subject is not None else "",
                props=props,
                source=Event.Source.SERVER,
            )
    except Exception:
        logger.exception("analytics: failed to record a %s event", name)
