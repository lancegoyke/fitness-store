"""Web push for the athlete PWA (Phase 4b — decisions S3/S7).

The push peer of ``notifications.emails.send_week_delivered_email``: when a coach
delivers a week, the athlete's subscribed devices get a "your week is ready" push
that deep-links to ``/meso/me/``. Signing uses VAPID (``pywebpush``); the keys
live in ``settings.MESO_VAPID_*``.

Graceful degradation is the contract — with no VAPID keys configured, every send
is a silent no-op (subscriptions are still *stored*, nothing is *sent*), so the
app boots and CI runs without creds, exactly like the delivery email skips an
athlete with no address. Sending is best-effort: a dead subscription (the push
service answers 404/410 Gone) is pruned; any other failure is swallowed and
logged so a delivery never fails on a bounced push.
"""

import json
import logging

from django.conf import settings
from pywebpush import WebPushException
from pywebpush import webpush

logger = logging.getLogger(__name__)

# Push services reject stale messages; expire the "your week is ready" nudge
# after a day rather than have it surface long after it's relevant.
DEFAULT_TTL_SECONDS = 60 * 60 * 24


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
    )
    return True


def _is_gone(exc):
    """A 404/410 from the push service means the subscription is dead."""
    response = getattr(exc, "response", None)
    return response is not None and response.status_code in (404, 410)


def notify_week_delivered(*, athlete, coach, plan, week, home_url):
    """Push a delivery notification to all the athlete's devices (best-effort).

    Returns the number of devices actually pushed to. A no-op (returns 0) when
    push is disabled or the athlete has no subscriptions. Dead subscriptions are
    deleted; other per-device failures are logged and skipped — one bad endpoint
    never blocks the others, and nothing here ever raises to the caller.
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
        "title": "Your next training week is ready",
        "body": f"{coach.display_name()} delivered {_week_label(week)} of {plan.title}.",
        "url": home_url,
        "tag": f"meso-week-{week.pk}",
    }

    sent = 0
    for subscription in subscriptions:
        try:
            if send_web_push(subscription.as_subscription_info(), payload):
                sent += 1
        except WebPushException as exc:
            if _is_gone(exc):
                subscription.delete()
            else:
                logger.warning(
                    "Web push failed for subscription %s: %s", subscription.pk, exc
                )
        except Exception:  # never let a bad push fail a delivery
            logger.exception(
                "Unexpected error pushing to subscription %s", subscription.pk
            )
    return sent


def _week_label(week):
    return f"Week {week.index}"
