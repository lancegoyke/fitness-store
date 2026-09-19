"""The web push ledger (#509 slice 3).

Parallel to ``notifications.ses_events``: the ``notifications`` app owns the
ledger (this module, plus ``PushKind``/``PushNotification`` in ``models.py``),
while ``meso.push`` owns the transport (signing and actually sending the
encrypted payload via ``pywebpush``). ``meso.push._fan_out`` is the only
caller of ``log_push_sent``/``log_push_error``; the athlete's landing page
(``meso.views.AthleteHomeView``) is the only caller of ``record_push_click``.

Every write here follows the same rule as ``analytics.track.track``: it runs
in its own ``transaction.atomic()`` savepoint, inside a ``try/except`` that
never re-raises. ``log_push_sent``/``log_push_error`` are called from a
delivery's ``on_commit`` callback (see ``meso.push.notify_block_delivered``)
and ``record_push_click`` from an ordinary page GET — in neither place may a
ledger hiccup cost the athlete their notification or break the page that
happens to carry ``?n=``. On PostgreSQL a failed statement aborts the whole
transaction it ran in; confining the failure to a savepoint (rather than just
catching the exception) keeps the caller's own transaction alive.
"""

import logging
import uuid
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from django.db import transaction
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.track import track

from .models import PushNotification

logger = logging.getLogger(__name__)


def log_push_sent(*, kind, user):
    """Open a ledger row for one push about to go out; ``None`` if it couldn't.

    Called *before* the push is actually sent, from ``meso.push._fan_out``,
    once per subscription: each device gets its own row (and so its own id),
    because each device's payload carries a different ``?n=`` and a click
    from one device must not silently mark another device's row clicked.

    Best-effort, the same savepoint idea as ``analytics.track.track`` — the
    caller is a delivery's ``on_commit`` callback, and a ledger that can't be
    written must never cost the athlete their notification. Returns the row
    so the caller can put its id in that device's payload URL and, later,
    stamp an error onto it if the send fails.
    """
    try:
        with transaction.atomic():
            return PushNotification.objects.create(kind=kind, user=user)
    except Exception:
        logger.exception(
            "notifications: failed to open a push ledger row (kind=%s)", kind
        )
        return None


def log_push_error(record, error):
    """Record why the push service rejected this send. Best-effort, never raises.

    ``record`` is whatever ``log_push_sent`` returned — ``None`` when opening
    the row itself already failed, in which case there's nothing to update
    and this is a no-op. ``error`` is truncated to the column width the same
    way ``notifications.ses_events._fit`` truncates SES payload fields: the
    caller (``meso.push._fan_out``) builds it from whatever the push service
    or ``pywebpush`` handed back, which isn't bounded to our column.
    """
    if record is None:
        return
    try:
        with transaction.atomic():
            max_length = PushNotification._meta.get_field("error").max_length
            PushNotification.objects.filter(pk=record.pk).update(
                error=str(error)[:max_length]
            )
    except Exception:
        logger.exception(
            "notifications: failed to record a push error for %s", record.pk
        )


def url_with_notification(url, record):
    """``url`` with ``?n=<record.pk>`` added — the click's return path.

    Returns ``url`` unchanged when there's no row (``log_push_sent`` failed).
    Merges into any query the URL already carries rather than assuming there
    is none — ``/meso/me/`` is the only target today, but nothing here should
    depend on that staying true.
    """
    if record is None:
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "n"]
    query.append(("n", str(record.pk)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def record_push_click(request):
    """Mark the ledger row named by ``?n=`` clicked, once, and emit the event.

    The click is recorded here, on the landing page (``meso.views.
    AthleteHomeView``), rather than from the service worker: a worker has no
    CSRF token and no session cookie it can be trusted with, and a public
    unauthenticated click endpoint would be a free counter for anyone. The
    notification's target URL already carries the id, so the click is just an
    ordinary authenticated GET by the person who was pushed.

    Only the row's own owner can mark it, and only from ``NULL``: an id that
    is unknown, malformed, or someone else's is ignored in silence, and a
    reload or a shared link doesn't count twice — the write is a conditional
    ``UPDATE ... WHERE clicked_at IS NULL AND user_id = <the caller>``, so two
    simultaneous loads (two tabs, a prefetch and a real open) still record
    exactly one click between them. ``track()`` only fires when that UPDATE
    actually changed a row, with the default ``source=server``: the server
    observed this one, in a request it served — only a beacon post
    (``analytics.views.track_beacon``) is ``source=client``.

    Returns ``True`` when it recorded a click, ``False`` otherwise (no ``n``,
    a bad id, someone else's row, or one already clicked). The whole body is
    wrapped so a ledger failure never 500s the athlete's home page.
    """
    try:
        if not request.user.is_authenticated:
            return False
        raw = request.GET.get("n")
        if not raw:
            return False
        try:
            notification_id = uuid.UUID(raw)
        except (ValueError, AttributeError, TypeError):
            return False

        with transaction.atomic():
            # Read first so a hit can carry `kind` on the event and a miss
            # (not found / already clicked) short-circuits without an UPDATE.
            # The UPDATE itself is what actually makes the click safe under a
            # race (two simultaneous loads): only the one that flips
            # clicked_at from NULL wins, so this read is just to decide
            # whether to bother and what to log.
            row = PushNotification.objects.filter(
                pk=notification_id, user=request.user
            ).first()
            if row is None or row.clicked_at is not None:
                return False
            updated = PushNotification.objects.filter(
                pk=notification_id,
                user=request.user,
                clicked_at__isnull=True,
            ).update(clicked_at=timezone.now())
        if updated != 1:
            return False

        track(EventName.PUSH_CLICKED, actor=request.user, subject=row, kind=row.kind)
        return True
    except Exception:
        logger.exception("notifications: failed to record a push click")
        return False
