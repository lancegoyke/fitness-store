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
  Passing ``trial_end`` defers Stripe's first charge to that date (#555) — a
  coach subscribing mid-trial keeps the rest of it instead of being charged
  immediately.
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


def create_subscription_checkout_session(
    coach, *, success_url, cancel_url, trial_end=None
):
    """A subscription Checkout Session for the flat Pro plan (the coach subscribes).

    ``trial_end`` (a datetime), when given, defers Stripe's first charge to it
    (#555) — a coach subscribing during their local trial keeps the rest of it
    instead of paying immediately. Omitted entirely (not even an empty dict)
    when there's nothing to defer, so a plain subscribe behaves exactly as
    before.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    customer = stripe_customer_get_or_create(coach)
    kwargs = {}
    if trial_end is not None:
        kwargs["subscription_data"] = {"trial_end": int(trial_end.timestamp())}
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.id,
        client_reference_id=str(coach.id),
        line_items=[{"price": settings.MESO_PRO_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        **kwargs,
    )


def create_billing_portal_session(coach, *, return_url):
    """A hosted Customer Portal session — the coach manages their subscription."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe.billing_portal.Session.create(
        customer=coach.stripe_customer_id,
        return_url=return_url,
    )


#: Stripe subscription statuses that mean the subscription is over for good.
#: Everything else — trialing, active, past_due, incomplete, unpaid, paused
#: (our own webhook's ``_STATUS_MAP`` maps unpaid/incomplete/paused-adjacent
#: statuses to local ``past_due``), or any status this code doesn't recognize
#: yet — counts as an open subscription and blocks a new Checkout (#556, item
#: 2): fail closed rather than let an unrecognized status slip a coach into a
#: second subscription.
ENDED_SUBSCRIPTION_STATUSES = frozenset({"canceled", "incomplete_expired"})


def customer_has_open_subscription(coach):
    """Does this coach already have a live Stripe subscription, checked directly?

    The local ``CoachSubscription`` mirror only updates once the webhook
    lands, which can lag a Checkout completion by a few seconds — long enough
    for a double-click, or an older Checkout tab opened before the coach's
    first subscription existed, to complete a second one before the mirror
    would have caught it (#556, item 2). ``billing_subscribe`` calls this
    right before it would open a new Checkout, so the check is against Stripe
    itself rather than the (possibly stale) mirror.

    A coach with no ``stripe_customer_id`` has never entered a card, so they
    can't have a subscription — this returns False without making a Stripe
    call. Otherwise every subscription for the customer is listed
    (``status="all"``, so canceled ones are included) and this returns True
    if **any** of them is outside ``ENDED_SUBSCRIPTION_STATUSES`` — trialing,
    active, past_due, incomplete, unpaid, paused, and any status this code
    doesn't recognize yet all block. Fail closed: an unrecognized status is
    treated as open rather than assumed harmless.

    Verified in Stripe test mode (2026-09-19): listing subscriptions for a
    customer id Stripe doesn't recognize raises ``stripe.error.InvalidRequestError``
    with ``code == "resource_missing"`` and ``param == "customer"`` — treated
    here as "no subscription" (a missing customer can't hold one, and
    ``stripe_customer_get_or_create`` recreates the customer at checkout
    anyway). Any other exception propagates, so the caller can fail closed
    instead of silently opening a Checkout.
    """
    if not coach.stripe_customer_id:
        return False
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        subscriptions = stripe.Subscription.list(
            customer=coach.stripe_customer_id, status="all", limit=100
        )
    except stripe.error.InvalidRequestError as e:
        if e.code == "resource_missing" and e.param == "customer":
            return False
        raise
    return any(
        sub.status not in ENDED_SUBSCRIPTION_STATUSES
        for sub in subscriptions.auto_paging_iter()
    )


def expire_open_subscription_checkouts(coach):
    """Expire the customer's other open subscription Checkout Sessions (#556, item 2).

    ``customer_has_open_subscription`` only runs at Checkout-creation time, so
    it can't stop an *older* Checkout tab that was already open before the
    coach's first subscription existed — that tab's session is still "open"
    and could still be completed, creating a second subscription. Calling
    this right before opening a new Checkout expires every other open
    ``mode="subscription"`` session for the customer, so only the newest
    Checkout can ever complete. A ``mode="payment"`` session (e.g. a store
    purchase open in another tab) is left alone — it isn't a subscription.

    A coach with no ``stripe_customer_id`` has no Checkout Sessions to expire.
    Any exception propagates — the caller fails closed rather than opening a
    new Checkout it can't be sure is the only one that can complete.
    """
    if not coach.stripe_customer_id:
        return
    stripe.api_key = settings.STRIPE_SECRET_KEY
    sessions = stripe.checkout.Session.list(
        customer=coach.stripe_customer_id, status="open", limit=100
    )
    for session in sessions.auto_paging_iter():
        if session.mode == "subscription":
            stripe.checkout.Session.expire(session.id)
