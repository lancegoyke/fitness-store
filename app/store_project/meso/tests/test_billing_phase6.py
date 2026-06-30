"""S6 — billing, Phase 6: base + per-seat pricing (TrainHeroic-style, D13).

Phase 2 shipped a single per-seat subscription Price. Phase 6 converts it into a
**two-line-item** subscription: a flat **base** Price (``MESO_BASE_PRICE_ID``,
quantity 1, $9.99/mo) billed alongside the **per-seat** Price
(``MESO_SEAT_PRICE_ID``, quantity = active seats, $1/mo).

The conversion touches three seams:

- ``stripe_gateway.create_subscription_checkout_session`` — Checkout now carries
  **both** line items (base before seat);
- ``webhooks._sync_from_subscription`` — a subscription now reports two items, so
  the handler classifies them by Price id (the seat item drives
  ``stripe_item_id`` + ``quantity``; the base item is recorded on the new
  ``stripe_base_item_id``), with a legacy single-item fallback;
- ``sync_seat_quantity`` / ``reconcile_seats`` — resize **only** the seat item;
  the base line stays fixed at quantity 1.

Plus the paywall copy ("$9.99/mo + $1 per active athlete") and the subscribe-view
guard requiring **both** Prices configured before opening Checkout (ships dormant
otherwise). Stripe is mocked throughout — no live access (``docs/meso/billing-plan.md``).
"""

from unittest import mock

import pytest
from django.test import Client

from store_project.meso.billing import stripe_gateway
from store_project.meso.billing import webhooks as billing_webhooks
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


GATEWAY_CHECKOUT = (
    "store_project.meso.billing.stripe_gateway.stripe.checkout.Session.create"
)
GATEWAY_SUB_MODIFY = (
    "store_project.meso.billing.stripe_gateway.stripe.Subscription.modify"
)


def _both_prices(settings):
    settings.MESO_BASE_PRICE_ID = "price_base_test"
    settings.MESO_SEAT_PRICE_ID = "price_seat_test"


# ---------------------------------------------------------------------------
# Checkout — two line items (base + per-seat)
# ---------------------------------------------------------------------------


class TestCheckoutTwoLineItems:
    def test_checkout_includes_base_and_seat_line_items(self, settings):
        _both_prices(settings)
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        line_items = create.call_args.kwargs["line_items"]
        # Base first (quantity 1, never resized), then the per-seat item (= 2 active).
        assert line_items == [
            {"price": "price_base_test", "quantity": 1},
            {"price": "price_seat_test", "quantity": 2},
        ]

    def test_seat_quantity_floors_at_one_with_base(self, settings):
        _both_prices(settings)
        coach = UserFactory()  # zero active links
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://x")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        line_items = create.call_args.kwargs["line_items"]
        assert line_items == [
            {"price": "price_base_test", "quantity": 1},
            {"price": "price_seat_test", "quantity": 1},
        ]

    def test_falls_back_to_seat_only_when_base_unconfigured(self, settings):
        """Defensive: a missing base Price degrades to the Phase-2 seat-only Checkout."""
        settings.MESO_BASE_PRICE_ID = ""
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://x")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        assert create.call_args.kwargs["line_items"] == [
            {"price": "price_seat_test", "quantity": 1}
        ]


# ---------------------------------------------------------------------------
# Webhook — classify the seat vs base subscription item
# ---------------------------------------------------------------------------


def _coach_with_customer(customer="cus_p6"):
    coach = UserFactory()
    coach.stripe_customer_id = customer
    coach.save(update_fields=["stripe_customer_id"])
    return coach


def _two_item_event(
    type_,
    *,
    customer="cus_p6",
    sub_id="sub_p6",
    base_price="price_base_test",
    seat_price="price_seat_test",
    base_item="si_base",
    seat_item="si_seat",
    seat_quantity=3,
    status="active",
):
    """A subscription event whose two items carry Price ids (base + per-seat)."""
    return {
        "type": type_,
        "data": {
            "object": {
                "id": sub_id,
                "customer": customer,
                "status": status,
                "current_period_end": 1900000000,
                "items": {
                    "data": [
                        {
                            "id": base_item,
                            "quantity": 1,
                            "price": {"id": base_price},
                        },
                        {
                            "id": seat_item,
                            "quantity": seat_quantity,
                            "price": {"id": seat_price},
                        },
                    ]
                },
            }
        },
    }


class TestWebhookClassifiesItems:
    def test_records_both_item_ids_and_seat_quantity(self, settings):
        _both_prices(settings)
        coach = _coach_with_customer()
        billing_webhooks.handle_event(
            _two_item_event("customer.subscription.created", seat_quantity=4)
        )
        sub = CoachSubscription.objects.get(coach=coach)
        # The per-seat item drives stripe_item_id + quantity; the base item is its
        # own field. Order-independent: classified by Price id, not list position.
        assert sub.stripe_item_id == "si_seat"
        assert sub.stripe_base_item_id == "si_base"
        assert sub.quantity == 4

    def test_classification_is_order_independent(self, settings):
        """The seat item is found by Price id even when Stripe lists it first."""
        _both_prices(settings)
        coach = _coach_with_customer()
        event = _two_item_event("customer.subscription.created")
        # Swap so the seat item is data[0] and the base item is data[1].
        items = event["data"]["object"]["items"]["data"]
        items.reverse()
        billing_webhooks.handle_event(event)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.stripe_item_id == "si_seat"
        assert sub.stripe_base_item_id == "si_base"

    def test_legacy_single_item_subscription_is_treated_as_the_seat(self, settings):
        """A pre-Phase-6 single-line sub (no Price match) still maps to the seat item."""
        _both_prices(settings)
        coach = _coach_with_customer()
        event = {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_legacy",
                    "customer": "cus_p6",
                    "status": "active",
                    "current_period_end": 1900000000,
                    "items": {"data": [{"id": "si_only", "quantity": 2}]},
                }
            },
        }
        billing_webhooks.handle_event(event)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.stripe_item_id == "si_only"
        assert sub.stripe_base_item_id == ""
        assert sub.quantity == 2


# ---------------------------------------------------------------------------
# Seat sync — resize only the per-seat item, never the base line
# ---------------------------------------------------------------------------


class TestSeatSyncIgnoresBaseItem:
    def test_sync_modifies_only_the_seat_item(self):
        sub = CoachSubscriptionFactory(
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_123",
            stripe_item_id="si_seat",
            stripe_base_item_id="si_base",
            quantity=1,
        )
        coach = sub.coach
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is True
        # Only the seat item is in the modify payload — the base line is untouched.
        assert modify.call_args.kwargs["items"] == [{"id": "si_seat", "quantity": 2}]


# ---------------------------------------------------------------------------
# Subscribe view — requires BOTH Prices configured (ships dormant otherwise)
# ---------------------------------------------------------------------------


class TestSubscribeRequiresBothPrices:
    URL = "/meso/billing/subscribe/"

    def _coach_client(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        return coach, c

    def test_missing_base_price_redirects_gracefully(self, settings):
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        settings.MESO_BASE_PRICE_ID = ""
        coach, c = self._coach_client()
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session"
        ) as create:
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/"
        create.assert_not_called()

    def test_both_prices_set_reaches_checkout(self, settings):
        _both_prices(settings)
        coach, c = self._coach_client()
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
            return_value=mock.Mock(url="https://stripe/checkout"),
        ) as create:
            resp = c.post(self.URL)
        assert resp.url == "https://stripe/checkout"
        create.assert_called_once()


# ---------------------------------------------------------------------------
# Paywall copy — "$9.99/mo + $1 per active athlete"
# ---------------------------------------------------------------------------


class TestPaywallPricingCopy:
    def test_billing_state_exposes_the_price_summary(self):
        from store_project.meso import presenters

        coach = UserFactory()
        state = presenters.billing_state(coach)
        assert state["price_summary"] == presenters.PRICE_SUMMARY
        assert "$9.99" in state["price_summary"]
        assert "$1" in state["price_summary"]

    def test_become_coach_page_shows_base_and_seat_pricing(self):
        resp = Client().get("/meso/coach/")
        body = resp.content.decode()
        assert "$9.99" in body
        assert "$1" in body

    def test_roster_free_plan_card_shows_pricing(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        resp = c.get("/meso/")
        body = resp.content.decode()
        assert "$9.99" in body
        assert "$1" in body
