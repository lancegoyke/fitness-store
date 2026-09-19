"""S6 — billing, Stripe (Checkout + Portal + webhook) under the flat plan (D14).

The flat monthly Pro plan (D14) bills a **single** recurring Price
(``MESO_PRO_PRICE_ID``, quantity 1) — no per-seat line, so the old seat-quantity
sync + daily ``reconcile_seats`` sweep are gone. What remains is:

- ``billing/stripe_gateway.py`` — thin, mockable wrappers over the ``stripe`` SDK:
  a subscription-mode Checkout Session (one flat line item) and a hosted Customer
  Portal Session;
- ``billing/webhooks.py`` — a clean, idempotent handler that materializes /
  updates a coach's ``CoachSubscription`` from the subscription + invoice events
  (the messy ``payments`` products webhook is left untouched);
- the ``billing_subscribe`` / ``billing_portal`` / ``billing_webhook`` views.

Stripe is mocked throughout (no network). See ``docs/meso/billing-plan.md``.
"""

import hashlib
import hmac
import importlib.util
import json
import time
from unittest import mock

import pytest
import stripe
from django.test import Client

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
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
GATEWAY_PORTAL = (
    "store_project.meso.billing.stripe_gateway.stripe.billing_portal.Session.create"
)


# ---------------------------------------------------------------------------
# stripe_gateway — Checkout
# ---------------------------------------------------------------------------


class TestCheckoutSession:
    def test_creates_a_subscription_session_with_one_flat_price(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = UserFactory()
        # The flat plan bills the same regardless of the athlete count.
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            session = stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        assert session.url == "https://stripe/cs"
        kwargs = create.call_args.kwargs
        assert kwargs["mode"] == "subscription"
        assert kwargs["client_reference_id"] == str(coach.id)
        assert kwargs["success_url"] == "https://x/ok"
        assert kwargs["cancel_url"] == "https://x/no"
        # One flat line item, quantity 1 — never the seat count.
        assert kwargs["line_items"] == [{"price": "price_pro_test", "quantity": 1}]


# ---------------------------------------------------------------------------
# stripe_gateway — Customer Portal
# ---------------------------------------------------------------------------


class TestPortalSession:
    def test_creates_a_portal_session_for_the_customer(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_existing"
        coach.save(update_fields=["stripe_customer_id"])
        with mock.patch(
            GATEWAY_PORTAL, return_value=mock.Mock(url="https://portal")
        ) as create:
            session = stripe_gateway.create_billing_portal_session(
                coach, return_url="https://x/back"
            )
        assert session.url == "https://portal"
        kwargs = create.call_args.kwargs
        assert kwargs["customer"] == "cus_existing"
        assert kwargs["return_url"] == "https://x/back"


# ---------------------------------------------------------------------------
# webhooks — the idempotent handler
# ---------------------------------------------------------------------------


def _sub_event(
    type_,
    *,
    customer="cus_hook",
    sub_id="sub_1",
    item_id="si_1",
    status="active",
    period_end=1900000000,
    cancellation_details=None,
):
    obj = {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "current_period_end": period_end,
        "items": {"data": [{"id": item_id}]},
    }
    if cancellation_details is not None:
        obj["cancellation_details"] = cancellation_details
    return {"type": type_, "data": {"object": obj}}


def _invoice_event(type_, *, customer="cus_hook", sub_id="sub_1"):
    return {
        "type": type_,
        "data": {"object": {"customer": customer, "subscription": sub_id}},
    }


def _coach_with_customer(customer="cus_hook"):
    coach = UserFactory()
    coach.stripe_customer_id = customer
    coach.save(update_fields=["stripe_customer_id"])
    return coach


def _events(name):
    """Every ``Event`` row of ``name``, oldest first (deterministic order)."""
    return list(Event.objects.filter(name=name).order_by("id"))


def _real_construct_event():
    """A pristine ``stripe.Webhook.construct_event``, bypassing the test Mock.

    ``config/settings/test.py`` replaces ``stripe.Webhook.construct_event`` with
    a ``Mock`` at import time — a class-attribute assignment, so it's mutated
    for the whole process, not just one module. ``importlib.import_module``
    would just hand back that same mutated, cached module. Building a fresh
    module object from the same spec and executing it re-runs ``stripe/
    _webhook.py`` from source into a brand-new module (never registered in
    ``sys.modules``), which defines its own, real ``Webhook`` class untouched
    by the mock — verified to still construct genuine ``stripe.Event``/
    ``stripe.Subscription`` objects (the classes themselves come from
    ``stripe._event``/``stripe._subscription``, which were never mutated).
    """
    spec = importlib.util.find_spec("stripe._webhook")
    real_webhook_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_webhook_module)
    return real_webhook_module.Webhook.construct_event


# ---------------------------------------------------------------------------
# Real ``stripe.Event``/``stripe.Subscription``/``stripe.Invoice`` builders (#543)
#
# stripe 15's ``Webhook.construct_event`` doesn't hand ``handle_event`` a plain
# dict — it hands back library objects (``StripeObject`` subclasses) that have
# no ``.get()``. ``_sub_event``/``_invoice_event`` above build the plain dicts
# the *old* (buggy) code path assumed; these build the *real* shape, from the
# same fields, so the handler tests below exercise what production actually
# sees. Each one asserts the constructed object's type so a regression back to
# plain dicts fails loudly instead of silently passing.
# ---------------------------------------------------------------------------


def _real_sub_event(
    type_,
    *,
    customer="cus_hook",
    sub_id="sub_1",
    item_id="si_1",
    status="active",
    period_end=1900000000,
    cancellation_details=None,
):
    obj = {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "current_period_end": period_end,
        "items": {
            "object": "list",
            "data": [{"id": item_id, "object": "subscription_item"}],
        },
    }
    if cancellation_details is not None:
        obj["cancellation_details"] = cancellation_details
    event = stripe.Event.construct_from(
        {"id": "evt_test", "object": "event", "type": type_, "data": {"object": obj}},
        "sk_test",
    )
    assert type(event["data"]["object"]) is stripe.Subscription
    return event


def _real_invoice_event(type_, *, customer="cus_hook", sub_id="sub_1"):
    obj = {"object": "invoice", "customer": customer, "subscription": sub_id}
    event = stripe.Event.construct_from(
        {"id": "evt_test", "object": "event", "type": type_, "data": {"object": obj}},
        "sk_test",
    )
    assert type(event["data"]["object"]) is stripe.Invoice
    return event


class TestWebhookHandler:
    def test_subscription_created_materializes_an_active_subscription(self):
        coach = _coach_with_customer()
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", item_id="si_flat")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"
        assert sub.stripe_item_id == "si_flat"
        assert sub.current_period_end is not None

    def test_subscription_updated_upgrades_an_existing_free_row(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.FREE)
        billing_webhooks.handle_event(_sub_event("customer.subscription.updated"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert CoachSubscription.objects.filter(coach=coach).count() == 1

    def test_subscription_updated_past_due(self):
        coach = _coach_with_customer()
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", status="past_due")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE

    def test_subscription_deleted_cancels(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.deleted", status="canceled")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED

    def test_invoice_payment_failed_marks_past_due(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_invoice_event("invoice.payment_failed"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE

    def test_invoice_paid_restores_active(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_invoice_event("invoice.paid"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE

    def test_is_idempotent(self):
        coach = _coach_with_customer()
        event = _sub_event("customer.subscription.updated", item_id="si_x")
        billing_webhooks.handle_event(event)
        billing_webhooks.handle_event(event)
        assert CoachSubscription.objects.filter(coach=coach).count() == 1
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.stripe_item_id == "si_x"

    def test_stale_delete_for_an_old_subscription_does_not_regress(self):
        """A late delete/cancel for a subscription the coach already replaced is ignored."""
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_new",
            stripe_item_id="si_new",
        )
        # A stale delete arrives for the *old* subscription id.
        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.deleted", sub_id="sub_old", status="canceled"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        # The active, current subscription is untouched.
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_new"

    def test_stale_live_update_for_an_old_subscription_is_ignored(self):
        """A retried active update for the *old* id can't clobber a live newer sub."""
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_new",
            stripe_item_id="si_new",
        )
        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.updated", sub_id="sub_old", status="active"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.stripe_subscription_id == "sub_new"
        assert sub.stripe_item_id == "si_new"

    def test_stale_live_update_does_not_replace_a_past_due_current_sub(self):
        """A retried active event for an old id can't override the current past_due sub."""
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_current",
            stripe_item_id="si_current",
        )
        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.updated", sub_id="sub_old", status="active"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE
        assert sub.stripe_subscription_id == "sub_current"

    def test_invoice_paid_does_not_resurrect_a_canceled_subscription(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_invoice_event("invoice.paid"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED

    def test_invoice_payment_failed_does_not_touch_a_canceled_subscription(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_invoice_event("invoice.payment_failed"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED

    def test_a_new_live_subscription_takes_over_from_a_canceled_one(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_old",
        )
        # Re-subscribe: a live event for a *new* subscription id takes over.
        billing_webhooks.handle_event(
            _sub_event(
                "customer.subscription.created", sub_id="sub_new2", status="active"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_new2"

    def test_unresolvable_customer_is_ignored(self):
        # No User has this customer id → no crash, nothing created.
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.updated", customer="cus_nobody")
        )
        assert CoachSubscription.objects.count() == 0

    def test_unhandled_event_type_is_a_noop(self):
        billing_webhooks.handle_event(
            {"type": "customer.created", "data": {"object": {}}}
        )
        assert CoachSubscription.objects.count() == 0


# ---------------------------------------------------------------------------
# webhooks — the handler against real stripe 15 library objects (#543)
#
# ``stripe.Webhook.construct_event`` hands ``handle_event`` a ``stripe.Event``
# whose ``data.object`` is a ``stripe.Subscription``/``stripe.Invoice`` — not a
# dict. On main these all raise ``AttributeError: get`` inside
# ``_sync_from_subscription``/``_nudge_status``/``_track_subscription_change``.
# ---------------------------------------------------------------------------


class TestWebhookHandlerRealStripeObjects:
    @pytest.mark.parametrize("preexisting", ["none", "trialing_no_stripe_id"])
    def test_subscription_created_materializes_an_active_subscription(
        self, preexisting
    ):
        coach = _coach_with_customer()
        if preexisting == "trialing_no_stripe_id":
            CoachSubscriptionFactory(
                coach=coach, status=CoachSubscription.Status.TRIALING
            )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.created", item_id="si_flat")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"
        assert sub.stripe_item_id == "si_flat"
        assert sub.current_period_end is not None

    def test_subscription_updated_to_past_due(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="past_due")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE

    def test_subscription_deleted_cancels(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.deleted", status="canceled")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED

    def test_invoice_payment_failed_marks_past_due(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_real_invoice_event("invoice.payment_failed"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE

    def test_invoice_paid_restores_active(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(_real_invoice_event("invoice.paid"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE


class TestWebhookAnalyticsRealStripeObjects:
    """#509 analytics driven by real stripe objects, replayed as production saw it."""

    def test_production_replay_gives_one_started_and_one_cancelled(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.TRIALING)

        # The exact order production delivered on 2026-09-17: an invoice.paid
        # before the mirror even knows about sub_1 (a no-op nudge — no local
        # past_due row to flip yet), then the subscription goes live, then a
        # replay of both.
        billing_webhooks.handle_event(_real_invoice_event("invoice.paid"))
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.created", status="active")
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.created", status="active")
        )
        billing_webhooks.handle_event(_real_invoice_event("invoice.paid"))

        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"
        started = _events(EventName.SUBSCRIPTION_STARTED)
        assert len(started) == 1
        assert started[0].props["subscription"] == "sub_1"

        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.deleted", status="canceled")
        )

        sub.refresh_from_db()
        assert sub.status == CoachSubscription.Status.CANCELED
        cancelled = _events(EventName.SUBSCRIPTION_CANCELLED)
        assert len(cancelled) == 1
        assert cancelled[0].props["subscription"] == "sub_1"
        # The replay didn't create a second started either.
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1


# ---------------------------------------------------------------------------
# billing_webhook view — signature verification
# ---------------------------------------------------------------------------


class TestWebhookView:
    URL = "/meso/billing/webhook/"

    def test_missing_signature_is_400(self):
        resp = Client().post(self.URL, data=b"{}", content_type="application/json")
        assert resp.status_code == 400

    def test_unset_secret_fails_closed(self, settings):
        """With no signing secret configured, a signed request is rejected (not verified vs "")."""
        settings.MESO_STRIPE_WEBHOOK_SECRET = ""
        coach = _coach_with_customer("cus_closed")
        with mock.patch(
            "store_project.meso.billing.webhooks.stripe.Webhook.construct_event"
        ) as construct:
            resp = Client().post(
                self.URL,
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=forged",
            )
        assert resp.status_code == 400
        # Stripe's verifier is never even reached, and nothing is mutated.
        construct.assert_not_called()
        assert not CoachSubscription.objects.filter(coach=coach).exists()

    def test_bad_signature_is_400(self):
        with mock.patch(
            "store_project.meso.billing.webhooks.stripe.Webhook.construct_event",
            side_effect=ValueError("bad"),
        ):
            resp = Client().post(
                self.URL,
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=deadbeef",
            )
        assert resp.status_code == 400

    def test_valid_event_is_handled_and_200(self):
        coach = _coach_with_customer("cus_view")
        event = _sub_event("customer.subscription.updated", customer="cus_view")
        with mock.patch(
            "store_project.meso.billing.webhooks.stripe.Webhook.construct_event",
            return_value=event,
        ):
            resp = Client().post(
                self.URL,
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=good",
            )
        assert resp.status_code == 200
        assert CoachSubscription.objects.filter(coach=coach).exists()

    def test_real_stripe_event_is_verified_and_handled(self, settings):
        """End-to-end through the view with a genuinely signed, genuine ``stripe.Event``.

        Unlike the test above (which hands ``handle_event`` a plain dict, the
        way the old buggy code assumed), this drives the view with a real,
        HMAC-signed payload verified by the *actual* Stripe signature checker,
        producing the real ``stripe.Event``/``stripe.Subscription`` objects
        production sees (#543). On main this raises ``AttributeError: get``
        inside ``handle_event`` — the Django test client re-raises view
        exceptions rather than turning them into a 500.
        """
        settings.MESO_STRIPE_WEBHOOK_SECRET = "whsec_test_543"
        coach = _coach_with_customer("cus_real_view")
        body = {
            "id": "evt_real_view",
            "object": "event",
            "type": "customer.subscription.created",
            "data": {
                "object": {
                    "id": "sub_real_view",
                    "object": "subscription",
                    "customer": "cus_real_view",
                    "status": "active",
                    "current_period_end": 1900000000,
                    "items": {
                        "object": "list",
                        "data": [{"id": "si_real_view", "object": "subscription_item"}],
                    },
                }
            },
        }
        payload = json.dumps(body)
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload}"
        sig = hmac.new(
            "whsec_test_543".encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        header = f"t={timestamp},v1={sig}"

        with (
            mock.patch(
                "store_project.meso.billing.webhooks.stripe.Webhook.construct_event",
                _real_construct_event(),
            ),
            mock.patch.object(
                billing_webhooks, "handle_event", wraps=billing_webhooks.handle_event
            ) as handle_spy,
        ):
            resp = Client().post(
                self.URL,
                data=payload.encode("utf-8"),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE=header,
            )

        assert resp.status_code == 200
        handle_spy.assert_called_once()
        (received_event,), _kwargs = handle_spy.call_args
        assert isinstance(received_event, stripe.Event)
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_real_view"


# ---------------------------------------------------------------------------
# billing_subscribe / billing_portal views
# ---------------------------------------------------------------------------


class TestSubscribeView:
    URL = "/meso/billing/subscribe/"

    def _coach_client(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        return coach, c

    def test_requires_login(self):
        resp = Client().post(self.URL)
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_redirects_to_stripe_checkout(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
            return_value=mock.Mock(url="https://stripe/checkout"),
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "https://stripe/checkout"

    def test_unconfigured_price_redirects_gracefully(self, settings):
        settings.MESO_PRO_PRICE_ID = ""
        coach, c = self._coach_client()
        resp = c.post(self.URL)
        # No 500 — bounced back to the roster with a message.
        assert resp.status_code == 302
        assert resp.url == "/meso/"

    def test_get_is_rejected(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        resp = c.get(self.URL)
        assert resp.status_code == 405

    def test_already_subscribed_coach_is_not_double_charged(self, settings):
        """A coach with a live Stripe subscription is bounced, not sent to a new Checkout."""
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_live",
            stripe_item_id="si_live",
        )
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session"
        ) as create:
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/"
        create.assert_not_called()

    def test_canceled_coach_can_resubscribe(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_dead",
        )
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
            return_value=mock.Mock(url="https://stripe/checkout"),
        ) as create:
            resp = c.post(self.URL)
        assert resp.url == "https://stripe/checkout"
        create.assert_called_once()


class TestPortalView:
    URL = "/meso/billing/portal/"

    def test_redirects_to_the_portal(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_p"
        coach.save(update_fields=["stripe_customer_id"])
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_billing_portal_session",
            return_value=mock.Mock(url="https://stripe/portal"),
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "https://stripe/portal"

    def test_without_a_customer_redirects_gracefully(self):
        coach = UserFactory()  # no stripe_customer_id
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/"
