"""Web push for the athlete PWA (Phase 4b — decisions S3/S7).

The push peer of ``notifications.emails.send_block_delivered_email``: when a
coach delivers a block, the athlete's subscribed devices get a "your block is
ready" push that deep-links to ``/meso/me/``. Signing uses VAPID
(``pywebpush``); the keys live in ``settings.MESO_VAPID_*``.

Graceful degradation is the contract — with no VAPID keys configured, every send
is a silent no-op (subscriptions are still *stored*, nothing is *sent*), so the
app boots and CI runs without creds, exactly like the delivery email skips an
athlete with no address. Sending is best-effort: a dead subscription (the push
service answers 404/410 Gone) is pruned; any other failure is swallowed and
logged so a delivery never fails on a bounced push.

**The ledger (#509 slice 3):** this module owns the transport; the
notifications app owns the ledger (``notifications.push``, backed by
``notifications.models.PushNotification`` — see that model's docstring for
why it's a dedicated table). ``_fan_out`` opens one ledger row per
subscription *before* sending, via ``notifications.push.log_push_sent``, and
stamps it with ``notifications.push.log_push_error`` if the send fails; each
device's payload URL carries its own row's id
(``notifications.push.url_with_notification``) so a later click on that
device can be attributed to it. Ledger writes are best-effort in their own
savepoint (see that module's docstring) — a ledger failure never costs the
athlete their push.
"""

import json
import logging

from django.conf import settings
from pywebpush import WebPushException
from pywebpush import webpush

from store_project.notifications import push as notifications_push
from store_project.notifications.models import PushKind

logger = logging.getLogger(__name__)

# Push services reject stale messages; expire the "your week is ready" nudge
# after a day rather than have it surface long after it's relevant.
DEFAULT_TTL_SECONDS = 60 * 60 * 24

# The send runs synchronously inside the deliver request's on_commit callback, so
# a slow or unresponsive push endpoint must not tie up the worker — cap the
# network wait so best-effort push can never hang a delivery.
PUSH_TIMEOUT_SECONDS = 10


def push_enabled():
    """True when VAPID keys are configured (otherwise sends are no-ops)."""
    return bool(settings.MESO_VAPID_PRIVATE_KEY and settings.MESO_VAPID_PUBLIC_KEY)


def vapid_public_key():
    """The base64url applicationServerKey the browser subscribes with."""
    return settings.MESO_VAPID_PUBLIC_KEY


def _vapid_claims():
    return {"sub": settings.MESO_VAPID_SUBJECT}


def send_web_push(subscription_info, payload, *, ttl=DEFAULT_TTL_SECONDS):
    """Send one encrypted push. Returns True if sent, raises on transport error.

    ``subscription_info`` is the browser subscription dict
    (``PushSubscription.as_subscription_info()``); ``payload`` is the JSON the
    service worker's ``push`` handler reads. Returns ``False`` when push is
    disabled (no keys). A ``WebPushException`` propagates so the caller can prune
    a 404/410 endpoint and swallow the rest.
    """
    if not push_enabled():
        return False
    webpush(
        subscription_info=subscription_info,
        data=json.dumps(payload),
        vapid_private_key=settings.MESO_VAPID_PRIVATE_KEY,
        vapid_claims=dict(_vapid_claims()),
        ttl=ttl,
        timeout=PUSH_TIMEOUT_SECONDS,
    )
    return True


def _is_gone(exc):
    """A 404/410 from the push service means the subscription is dead.

    Both attributes are read defensively: ``WebPushException`` carries a
    ``response`` only when the push service actually answered, and a
    transport-level failure builds one without a ``status_code``. Reading
    either one directly would raise an ``AttributeError`` out of
    ``_fan_out``'s ``except WebPushException`` block — skipping every
    remaining device in the fan-out over one odd failure.
    """
    return getattr(getattr(exc, "response", None), "status_code", None) in (404, 410)


def notify_block_delivered(*, athlete, coach, plan, mesocycle, week_count, home_url):
    """Push a block-delivery notification to the athlete's devices (best-effort).

    The deliver nudge (Meso P3; the per-week variant was retired with the 2d
    live+notify model): the deliver path nudges about a whole mesocycle at
    once, so the athlete gets one "your new block is ready" push, not one per
    week. A no-op (returns 0) when push is disabled or the athlete has no
    subscriptions, dead subscriptions are pruned, other per-device failures are
    logged and skipped, and nothing here ever raises to the caller. Returns the
    number of devices actually pushed to.
    """
    # Imported here to avoid a models import at module load (push.py is imported
    # from views before app loading settles in some paths).
    from .models import PushSubscription

    if not push_enabled():
        return 0

    subscriptions = list(PushSubscription.objects.filter(athlete=athlete))
    if not subscriptions:
        return 0

    payload = {
        "title": "Your new training block is ready",
        "body": (
            f"{coach.display_name()} delivered a new block "
            f"({_week_count_label(week_count)}) of {plan.title}."
        ),
        "url": home_url,
        "tag": f"meso-block-{mesocycle.pk}",
    }
    return _fan_out(subscriptions, payload, kind=PushKind.BLOCK_DELIVERED, user=athlete)


def _fan_out(subscriptions, payload, *, kind, user):
    """Send one ``payload`` to each subscription; return the count actually sent.

    The per-device loop behind the delivery notifier: dead endpoints
    (404/410 Gone) are pruned, any other per-device failure is logged and
    skipped, and nothing here ever raises — one bad endpoint never blocks the
    others or fails the deliver.

    Bails out before touching the ledger at all when push is disabled: a
    disabled ``send_web_push`` already no-ops below, but the ledger must never
    record a push that was never actually attempted, so the short-circuit
    happens here rather than relying on that no-op.

    Per subscription: a ledger row is opened *before* the send
    (``notifications.push.log_push_sent``), and that device's own copy of
    ``payload`` gets its own ``?n=<row id>`` URL
    (``notifications.push.url_with_notification``) — the shared ``payload``
    dict itself is never mutated, since every device must carry a different
    id. A ``WebPushException``/other failure is stamped onto that row
    (``notifications.push.log_push_error``) before the existing prune-or-log
    handling runs, unchanged.
    """
    if not push_enabled():
        return 0

    sent = 0
    for subscription in subscriptions:
        record = notifications_push.log_push_sent(kind=kind, user=user)
        try:
            # Inside the try with the send, not before it: building this
            # device's URL is ledger work, and a payload without a `url` or
            # one `urlsplit` chokes on would otherwise raise out of the loop
            # and cost every REMAINING device its push too — the one thing
            # "a ledger failure never stops a push" is supposed to rule out.
            device_payload = dict(payload)
            device_payload["url"] = notifications_push.url_with_notification(
                payload.get("url", ""), record
            )
            if send_web_push(subscription.as_subscription_info(), device_payload):
                sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            notifications_push.log_push_error(
                record, f"{status} {exc}" if status is not None else str(exc)
            )
            if _is_gone(exc):
                subscription.delete()
            else:
                logger.warning(
                    "Web push failed for subscription %s: %s", subscription.pk, exc
                )
        except Exception as exc:  # never let a bad push fail a delivery
            notifications_push.log_push_error(record, str(exc))
            logger.exception(
                "Unexpected error pushing to subscription %s", subscription.pk
            )
    return sent


def _week_count_label(week_count):
    """Pluralize-correct "N week(s)" for the block push copy."""
    return f"{week_count} week" + ("" if week_count == 1 else "s")
