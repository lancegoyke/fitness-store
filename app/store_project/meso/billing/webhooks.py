"""A clean, idempotent Stripe billing webhook handler (S6 billing, Phase 2, D9).

The store already has a products webhook (``payments.views.stripe_webhook``) —
one-time payments, debug prints, inline test-user creation. We leave it untouched
and handle *subscription* lifecycle here, on a separate endpoint with its own
signing secret (``MESO_STRIPE_WEBHOOK_SECRET``).

Stripe is the source of truth; this handler mirrors a coach's subscription state
into the local ``CoachSubscription`` so a request can gate without calling Stripe
(D8). It is **idempotent** — keyed by the coach (1:1), driven off the full
subscription object — so a replayed or out-of-order event converges to the same
row. Events handled:

- ``customer.subscription.created|updated`` — upsert from the subscription object
  (status, item id, quantity, period end).
- ``customer.subscription.deleted`` — the subscription is gone → ``canceled``
  (which gates identically to ``free``; the coach keeps read access, D6).
- ``invoice.payment_failed`` / ``invoice.paid`` — a belt-and-suspenders status
  nudge (past_due / active) keyed off the subscription id.

A coach we can't resolve (unknown Stripe customer) is logged and ignored — the
event isn't transient, so we don't want Stripe to retry it forever.
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model

from store_project.meso.models import CoachSubscription

logger = logging.getLogger(__name__)
User = get_user_model()

#: Stripe subscription status → local status. Our trial is local/no-card, so a
#: real subscription is created ``active``; the rest map defensively.
_STATUS_MAP = {
    "active": CoachSubscription.Status.ACTIVE,
    "trialing": CoachSubscription.Status.TRIALING,
    "past_due": CoachSubscription.Status.PAST_DUE,
    "unpaid": CoachSubscription.Status.PAST_DUE,
    "incomplete": CoachSubscription.Status.PAST_DUE,
    "canceled": CoachSubscription.Status.CANCELED,
    "incomplete_expired": CoachSubscription.Status.CANCELED,
}


def construct_event(payload, sig_header):
    """Verify a raw webhook against the billing endpoint secret → a Stripe event.

    **Fails closed** when the secret is unset: Stripe's verifier would otherwise
    check the signature against ``""``, which an attacker can HMAC-sign trivially —
    so in the dormant pre-configuration deploy state a forged event could drive
    subscription state. With no secret we reject before verifying.

    Raises ``ValueError`` (no secret / bad payload) or
    ``stripe.error.SignatureVerificationError`` (bad signature); the view turns
    any of these into a 400.
    """
    secret = settings.MESO_STRIPE_WEBHOOK_SECRET
    if not secret:
        raise ValueError("Billing webhook secret is not configured.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def handle_event(event):
    """Apply a verified billing event to the local mirror (idempotent)."""
    event_type = event["type"]
    obj = event["data"]["object"]
    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _sync_from_subscription(obj, deleted=event_type.endswith("deleted"))
    elif event_type == "invoice.payment_failed":
        _nudge_status(obj, CoachSubscription.Status.PAST_DUE)
    elif event_type == "invoice.paid":
        _nudge_status(obj, CoachSubscription.Status.ACTIVE)
    # Anything else is intentionally ignored.


def _ts_to_dt(ts):
    """A Stripe unix timestamp → an aware datetime (None passes through)."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=dt_timezone.utc)


def _coach_for_customer(customer_id):
    """The local coach behind a Stripe customer id, or None (logged) if unknown."""
    if not customer_id:
        return None
    coach = User.objects.filter(stripe_customer_id=customer_id).first()
    if coach is None:
        logger.warning("Billing webhook: no user for Stripe customer %s", customer_id)
    return coach


def _sync_from_subscription(sub_obj, *, deleted):
    """Upsert the coach's ``CoachSubscription`` from a Stripe subscription object."""
    coach = _coach_for_customer(sub_obj.get("customer"))
    if coach is None:
        return
    incoming_id = sub_obj.get("id", "")
    if deleted:
        status = CoachSubscription.Status.CANCELED
    else:
        status = _STATUS_MAP.get(
            sub_obj.get("status"), CoachSubscription.Status.PAST_DUE
        )
    # The mirror is keyed by coach (1:1), so an out-of-order event for an *older*
    # subscription the coach already replaced could otherwise clobber the newer
    # one. If we already track a different subscription, only a *live*
    # (active/trialing) event takes over — a stale delete/past_due/cancel for the
    # old id is ignored so it can't regress the current active subscription.
    existing = getattr(coach, "coach_subscription", None)
    if (
        existing
        and existing.stripe_subscription_id
        and existing.stripe_subscription_id != incoming_id
        and status not in CoachSubscription.ACTIVE_STATUSES
    ):
        logger.info(
            "Billing webhook: ignoring stale event for subscription %s "
            "(coach already on %s)",
            incoming_id,
            existing.stripe_subscription_id,
        )
        return
    items = (sub_obj.get("items") or {}).get("data") or [{}]
    item = items[0]
    CoachSubscription.objects.update_or_create(
        coach=coach,
        defaults={
            "status": status,
            "stripe_subscription_id": sub_obj.get("id", ""),
            "stripe_item_id": item.get("id", ""),
            "quantity": item.get("quantity") or 0,
            "current_period_end": _ts_to_dt(sub_obj.get("current_period_end")),
        },
    )


def _nudge_status(invoice_obj, status):
    """A thin status nudge from an invoice event, keyed by the subscription id.

    The authoritative state comes from the subscription events; this just keeps
    the mirror fresh between them (a failed/paid invoice flips past_due/active).
    Ignored if the subscription isn't mirrored locally yet.
    """
    sub_id = invoice_obj.get("subscription")
    if not sub_id:
        return
    updated = (
        CoachSubscription.objects.filter(stripe_subscription_id=sub_id)
        .exclude(status=status)
        .update(status=status)
    )
    if not updated:
        logger.info("Billing webhook: no mirror for subscription %s (invoice)", sub_id)
