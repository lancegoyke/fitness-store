"""Thin, mockable wrappers over the Stripe SDK (S6 billing, Phase 2; flat plan D14).

Everything that actually talks to Stripe lives here, so the views and the webhook
stay testable and the call surface is one small, mocked module. Billing is a
**single flat monthly Pro Price** (``MESO_PRO_PRICE_ID``, quantity 1 — the flat
plan, D14): one line item, never resized. There is no per-seat line, so there is
no seat-quantity sync (the old base+per-seat machinery — ``sync_seat_quantity`` /
``reconcile_seats`` — is gone; the flat plan bills the same regardless of the
coach's active-athlete count).

- ``create_subscription_checkout_session`` — a ``mode="subscription"`` Checkout
  Session the coach is redirected to in order to enter a card and subscribe.
- ``create_billing_portal_session`` — Stripe's hosted Customer Portal, where the
  coach updates their card / cancels / sees invoices (D7 — we hand-roll none of
  that UX).

The Stripe *customer* is the shared ``User.stripe_customer_id`` (reusing the
store's existing ``stripe_customer_get_or_create``), so a coach who already bought
a one-time product keeps the same customer.
"""

import stripe
from django.conf import settings

from store_project.payments.utils import stripe_customer_get_or_create


def create_subscription_checkout_session(coach, *, success_url, cancel_url):
    """A subscription Checkout Session for the flat Pro plan (the coach subscribes)."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    customer = stripe_customer_get_or_create(coach)
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.id,
        client_reference_id=str(coach.id),
        line_items=[{"price": settings.MESO_PRO_PRICE_ID, "quantity": 1}],
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
