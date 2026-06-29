"""Thin, mockable wrappers over the Stripe SDK (S6 billing, Phase 2).

Everything that actually talks to Stripe lives here, so the views and the sweep
stay testable and the call surface is one small, mocked module. Per-seat
subscription billing (D2/D7): one recurring Stripe Price (``MESO_SEAT_PRICE_ID``),
``quantity`` = the coach's active-athlete count.

- ``create_subscription_checkout_session`` — a ``mode="subscription"`` Checkout
  Session the coach is redirected to in order to enter a card and subscribe.
- ``create_billing_portal_session`` — Stripe's hosted Customer Portal, where the
  coach updates their card / cancels / sees invoices (D7 — we hand-roll none of
  that UX).
- ``sync_seat_quantity`` — best-effort: push the coach's current active-seat count
  to Stripe as the subscription item quantity. A no-op unless the coach has a live
  Stripe subscription; the daily ``reconcile_seats`` sweep is the backstop.

The Stripe *customer* is the shared ``User.stripe_customer_id`` (reusing the
store's existing ``stripe_customer_get_or_create``), so a coach who already bought
a one-time product keeps the same customer.
"""

import stripe
from django.conf import settings

from store_project.meso.models import CoachSubscription
from store_project.payments.utils import stripe_customer_get_or_create

from . import access


def _seat_quantity(coach):
    """The seat count to bill — the coach's active links, floored at one.

    A licensed subscription must bill at least one seat (Stripe rejects a
    ``quantity`` of 0), so a paid coach with zero active athletes still pays for
    one seat until they cancel.
    """
    return max(access.active_seat_count(coach), 1)


def create_subscription_checkout_session(coach, *, success_url, cancel_url):
    """A subscription Checkout Session for the per-seat plan (the coach subscribes)."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    customer = stripe_customer_get_or_create(coach)
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.id,
        client_reference_id=str(coach.id),
        line_items=[
            {"price": settings.MESO_SEAT_PRICE_ID, "quantity": _seat_quantity(coach)}
        ],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
    )


def create_billing_portal_session(coach, *, return_url):
    """A hosted Customer Portal session — the coach manages their subscription."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe.billing_portal.Session.create(
        customer=coach.stripe_customer_id,
        return_url=return_url,
    )


def sync_seat_quantity(coach):
    """Best-effort: push the coach's active-seat count to Stripe. Returns whether it changed.

    A no-op (returns ``False``) for a coach without a live Stripe subscription
    (free / local-trial / comped / no row) or one already in sync — only a coach
    who has actually subscribed (``stripe_subscription_id`` + ``stripe_item_id``
    set) is reconciled. The local ``quantity`` cache is updated only after Stripe
    accepts the change, so a failed push leaves the cache honest for the next
    sweep. Callers wrap this best-effort; the daily ``reconcile_seats`` sweep
    corrects any drift.
    """
    sub = getattr(coach, "coach_subscription", None)
    if not sub or not sub.stripe_subscription_id or not sub.stripe_item_id:
        return False
    # A canceled mirror keeps its Stripe ids for history, but the subscription is
    # dead — modifying it would just error. Only touch a *current* subscription
    # (active / past_due).
    if sub.status not in CoachSubscription.LIVE_STRIPE_STATUSES:
        return False
    quantity = _seat_quantity(coach)
    if quantity == sub.quantity:
        return False
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.Subscription.modify(
        sub.stripe_subscription_id,
        items=[{"id": sub.stripe_item_id, "quantity": quantity}],
        proration_behavior="create_prorations",
    )
    sub.quantity = quantity
    sub.save(update_fields=["quantity", "modified"])
    return True
