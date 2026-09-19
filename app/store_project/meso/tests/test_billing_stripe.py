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
from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest import mock

import pytest
import stripe
from django.contrib.messages import get_messages
from django.test import Client
from django.utils import dateformat
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso.billing import access as billing_access
from store_project.meso.billing import stripe_gateway
from store_project.meso.billing import webhooks as billing_webhooks
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import CoachProfileFactory
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachSubscription
from store_project.meso.views import CHECKOUT_PENDING_SESSION_KEY
from store_project.meso.views import CHECKOUT_STARTED_SESSION_KEY
from store_project.meso.views import STRIPE_UNAVAILABLE_MESSAGE
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _refetch(user):
    """A fresh copy of ``user``, bypassing Django's cached reverse-o2o accessor.

    ``access.*`` reads ``user.coach_subscription``; once that descriptor has
    been accessed on an instance it's cached, so a test that mutates the
    ``CoachSubscription`` row via a bare ``.update()`` (no signal, no cache
    invalidation) must re-fetch the coach to see the change.
    """
    return type(user).objects.get(pk=user.pk)


GATEWAY_CHECKOUT = (
    "store_project.meso.billing.stripe_gateway.stripe.checkout.Session.create"
)
GATEWAY_PORTAL = (
    "store_project.meso.billing.stripe_gateway.stripe.billing_portal.Session.create"
)
GATEWAY_SUB_LIST = "store_project.meso.billing.stripe_gateway.stripe.Subscription.list"
GATEWAY_SESSION_LIST = (
    "store_project.meso.billing.stripe_gateway.stripe.checkout.Session.list"
)
GATEWAY_SESSION_EXPIRE = (
    "store_project.meso.billing.stripe_gateway.stripe.checkout.Session.expire"
)
GATEWAY_SESSION_RETRIEVE = (
    "store_project.meso.billing.stripe_gateway.stripe.checkout.Session.retrieve"
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


class TestCheckoutSessionTrial:
    """Passing ``trial_end`` becomes Checkout's ``subscription_data`` (#555)."""

    def test_trial_end_becomes_subscription_data(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = UserFactory()
        trial_end = timezone.now() + timedelta(days=5)
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach,
                success_url="https://x/ok",
                cancel_url="https://x/no",
                trial_end=trial_end,
            )
        kwargs = create.call_args.kwargs
        assert kwargs["subscription_data"] == {"trial_end": int(trial_end.timestamp())}

    def test_no_trial_end_omits_subscription_data(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = UserFactory()
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        kwargs = create.call_args.kwargs
        assert "subscription_data" not in kwargs


# ---------------------------------------------------------------------------
# billing/access.py — deferred_first_charge (#555)
#
# Whether subscribing *right now* would defer the first Stripe charge to the
# local trial's end (rather than charging today) — Stripe Checkout requires
# ``subscription_data.trial_end`` to be at least 48 hours out.
# ---------------------------------------------------------------------------


class TestDeferredFirstCharge:
    def test_local_trial_with_ten_days_left_defers_to_trial_end(self):
        coach = UserFactory()
        trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        assert billing_access.deferred_first_charge(coach) == trial_end

    def test_just_past_the_minimum_plus_margin_defers(self):
        coach = UserFactory()
        trial_end = timezone.now() + timedelta(hours=48, minutes=6)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        assert billing_access.deferred_first_charge(coach) == trial_end

    def test_forty_seven_hours_left_charges_today(self):
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(hours=47),
        )
        assert billing_access.deferred_first_charge(coach) is None

    def test_inside_the_margin_charges_today(self):
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(hours=48, minutes=1),
        )
        assert billing_access.deferred_first_charge(coach) is None

    def test_lapsed_trial_is_none(self):
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(minutes=1),
        )
        assert billing_access.deferred_first_charge(coach) is None

    def test_free_row_is_none(self):
        coach = UserFactory()
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.FREE)
        assert billing_access.deferred_first_charge(coach) is None

    def test_no_row_is_none(self):
        coach = UserFactory()
        assert billing_access.deferred_first_charge(coach) is None

    def test_stripe_trial_is_none(self):
        """A trialing row that already has a Stripe subscription never re-defers."""
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=10),
            stripe_subscription_id="sub_1",
        )
        assert billing_access.deferred_first_charge(coach) is None

    def test_active_is_none(self):
        coach = UserFactory()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        assert billing_access.deferred_first_charge(coach) is None


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
# stripe_gateway — never open a second subscription (#556, item 2)
# ---------------------------------------------------------------------------


class TestCustomerHasOpenSubscription:
    @pytest.mark.parametrize(
        "status",
        ["trialing", "active", "past_due", "incomplete", "unpaid", "paused"],
    )
    def test_an_open_status_blocks(self, status):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        with mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list([status])):
            assert stripe_gateway.customer_has_open_subscription(coach) is True

    @pytest.mark.parametrize("statuses", [["canceled"], ["incomplete_expired"], []])
    def test_only_ended_or_no_subscriptions_do_not_block(self, statuses):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        with mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list(statuses)):
            assert stripe_gateway.customer_has_open_subscription(coach) is False

    def test_no_customer_id_returns_false_without_a_stripe_call(self):
        coach = UserFactory()  # blank stripe_customer_id
        with mock.patch(GATEWAY_SUB_LIST) as list_mock:
            assert stripe_gateway.customer_has_open_subscription(coach) is False
        list_mock.assert_not_called()

    def test_calls_list_with_the_expected_kwargs_and_enforces_the_real_signature(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        mock_list = mock.create_autospec(
            _real_subscription_list(), return_value=_subscription_list([])
        )
        with mock.patch(GATEWAY_SUB_LIST, mock_list):
            stripe_gateway.customer_has_open_subscription(coach)
        mock_list.assert_called_once_with(customer="cus_x", status="all", limit=100)
        # stripe 15's ``Subscription.list`` is keyword-only — a positional call
        # (the #548-style bug this autospec pattern guards against) is rejected.
        with pytest.raises(TypeError):
            mock_list("cus_x")

    def test_list_raising_propagates(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        with mock.patch(
            GATEWAY_SUB_LIST, side_effect=stripe.error.APIConnectionError("boom")
        ):
            with pytest.raises(stripe.error.APIConnectionError):
                stripe_gateway.customer_has_open_subscription(coach)

    def test_resource_missing_customer_reads_as_no_subscription(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_gone"
        coach.save(update_fields=["stripe_customer_id"])
        err = stripe.error.InvalidRequestError(
            "No such customer", "customer", code="resource_missing"
        )
        with mock.patch(GATEWAY_SUB_LIST, side_effect=err):
            assert stripe_gateway.customer_has_open_subscription(coach) is False

    def test_other_invalid_request_error_propagates(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        err = stripe.error.InvalidRequestError(
            "bad", "limit", code="parameter_invalid_integer"
        )
        with mock.patch(GATEWAY_SUB_LIST, side_effect=err):
            with pytest.raises(stripe.error.InvalidRequestError):
                stripe_gateway.customer_has_open_subscription(coach)

    def test_resource_missing_with_a_different_param_propagates(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        err = stripe.error.InvalidRequestError("x", "id", code="resource_missing")
        with mock.patch(GATEWAY_SUB_LIST, side_effect=err):
            with pytest.raises(stripe.error.InvalidRequestError):
                stripe_gateway.customer_has_open_subscription(coach)


class TestExpireOpenSubscriptionCheckouts:
    def test_expires_open_subscription_sessions_leaves_payment_sessions_alone(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        sessions = _session_list([("cs_sub", "subscription"), ("cs_pay", "payment")])
        with (
            mock.patch(GATEWAY_SESSION_LIST, return_value=sessions),
            mock.patch(GATEWAY_SESSION_EXPIRE) as expire,
        ):
            stripe_gateway.expire_open_subscription_checkouts(coach)
        expire.assert_called_once_with("cs_sub")

    def test_no_customer_id_is_a_noop(self):
        coach = UserFactory()
        with mock.patch(GATEWAY_SESSION_LIST) as list_mock:
            stripe_gateway.expire_open_subscription_checkouts(coach)
        list_mock.assert_not_called()

    def test_calls_list_with_the_expected_kwargs(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        with mock.patch(
            GATEWAY_SESSION_LIST, return_value=_session_list([])
        ) as list_mock:
            stripe_gateway.expire_open_subscription_checkouts(coach)
        list_mock.assert_called_once_with(customer="cus_x", status="open", limit=100)

    def test_expire_raising_propagates_when_the_session_is_still_open(self):
        """A genuinely unexpected expire failure still fails closed.

        The re-``retrieve`` fallback (below) only swallows the race where the
        session stopped being open out from under us — if it's still
        ``"open"``, whatever ``expire`` complained about is a real problem
        and must propagate.
        """
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        sessions = _session_list([("cs_sub", "subscription")])
        with (
            mock.patch(GATEWAY_SESSION_LIST, return_value=sessions),
            mock.patch(
                GATEWAY_SESSION_EXPIRE,
                side_effect=stripe.error.InvalidRequestError("gone", "id"),
            ),
            mock.patch(
                GATEWAY_SESSION_RETRIEVE,
                return_value=mock.Mock(status="open"),
            ),
        ):
            with pytest.raises(stripe.error.InvalidRequestError):
                stripe_gateway.expire_open_subscription_checkouts(coach)

    def test_no_customer_id_resource_missing_is_tolerated(self):
        """A coach whose Stripe customer was deleted isn't bounced forever.

        Mirrors the tolerance ``customer_has_open_subscription`` already
        gives a missing customer — before this fix the two disagreed.
        """
        coach = UserFactory()
        coach.stripe_customer_id = "cus_gone"
        coach.save(update_fields=["stripe_customer_id"])
        err = stripe.error.InvalidRequestError(
            "No such customer", "customer", code="resource_missing"
        )
        with (
            mock.patch(GATEWAY_SESSION_LIST, side_effect=err),
            mock.patch(GATEWAY_SESSION_EXPIRE) as expire,
        ):
            stripe_gateway.expire_open_subscription_checkouts(coach)
        expire.assert_not_called()

    def test_other_invalid_request_error_from_list_propagates(self):
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        err = stripe.error.InvalidRequestError(
            "bad", "limit", code="parameter_invalid_integer"
        )
        with mock.patch(GATEWAY_SESSION_LIST, side_effect=err):
            with pytest.raises(stripe.error.InvalidRequestError):
                stripe_gateway.expire_open_subscription_checkouts(coach)

    def test_session_no_longer_open_when_expired_is_tolerated(self):
        """The session raced closed between ``list`` and ``expire`` (#556 review).

        Its own 24h TTL, or another tab completing it, can flip a session out
        of ``"open"`` in that gap — Stripe's ``expire`` then raises instead of
        no-op'ing (``code`` is ``None``). The goal (no other completable
        session survives) already holds once it's not open any more, so this
        is tolerated rather than failing the whole subscribe.
        """
        coach = UserFactory()
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        sessions = _session_list(
            [("cs_raced", "subscription"), ("cs_still_open", "subscription")]
        )
        expire_err = stripe.error.InvalidRequestError(
            "Only Checkout Sessions with a status in [open] can be expired", None
        )

        def fake_expire(session_id):
            if session_id == "cs_raced":
                raise expire_err

        with (
            mock.patch(GATEWAY_SESSION_LIST, return_value=sessions),
            mock.patch(GATEWAY_SESSION_EXPIRE, side_effect=fake_expire) as expire,
            mock.patch(
                GATEWAY_SESSION_RETRIEVE,
                return_value=mock.Mock(status="complete"),
            ) as retrieve,
        ):
            stripe_gateway.expire_open_subscription_checkouts(coach)
        retrieve.assert_called_once_with("cs_raced")
        # The other, genuinely-still-open session was still expired.
        assert expire.call_args_list[-1].args == ("cs_still_open",)


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
# stripe 15's ``Webhook.construct_event`` hands ``handle_event`` library objects
# (``StripeObject`` subclasses) with no ``.get()``, not the plain dicts
# ``_sub_event``/``_invoice_event`` build. These build the same fields as real
# objects, and assert the type so a regression back to dicts fails loudly.
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
    trial_end=None,
    cancel_at=None,
    cancel_at_period_end=None,
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
    if trial_end is not None:
        obj["trial_end"] = trial_end
    if cancel_at is not None:
        obj["cancel_at"] = cancel_at
    if cancel_at_period_end is not None:
        obj["cancel_at_period_end"] = cancel_at_period_end
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


# ---------------------------------------------------------------------------
# Real ``stripe.ListObject`` builders + pristine (un-mocked) real methods (#556)
#
# ``config/settings/test.py`` replaces ``stripe.Subscription.list`` /
# ``stripe.checkout.Session.list`` / ``.expire`` with autospec'd Mocks at
# import time (a class-attribute assignment, mutated for the whole process —
# same situation ``_real_construct_event`` documents above). A test that wants
# to prove stripe-15's real, keyword-only signature is what's enforced needs
# the pristine method, not the already-mocked one, as the autospec's target.
# ---------------------------------------------------------------------------


def _subscription_list(statuses):
    """A real ``stripe.ListObject`` of subscriptions, one per status given."""
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "url": "/v1/subscriptions",
            "has_more": False,
            "data": [
                {"id": f"sub_{i}", "object": "subscription", "status": status}
                for i, status in enumerate(statuses)
            ],
        },
        "sk_test",
    )


def _session_list(sessions):
    """A real ``stripe.ListObject`` of Checkout Sessions from ``(id, mode)`` pairs."""
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "url": "/v1/checkout/sessions",
            "has_more": False,
            "data": [
                {"id": sid, "object": "checkout.session", "mode": mode}
                for sid, mode in sessions
            ],
        },
        "sk_test",
    )


def _real_subscription_list():
    """A pristine ``stripe.Subscription.list``, bypassing the test autospec mock."""
    spec = importlib.util.find_spec("stripe._subscription")
    real_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_module)
    return real_module.Subscription.list


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

        # Production has a created + an invoice.paid for one subscription
        # failing since 2026-09-17, and a replay can land in either order: an
        # invoice.paid before the mirror knows sub_1 (a no-op nudge — no local
        # past_due row to flip yet), then the subscription goes live, then a
        # second delivery of both.
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
# webhooks — a coach who subscribes mid-trial (#555): a Stripe-backed
# `trialing` row is a live subscription (`is_stripe_trial` /
# `has_live_stripe_subscription`), not a locally-clocked one.
# ---------------------------------------------------------------------------


class TestWebhookStripeTrialJourney:
    def test_subscribing_during_a_local_trial_keeps_access_through_to_active(self):
        coach = _coach_with_customer()
        local_trial_end = (timezone.now() + timedelta(days=10)).replace(microsecond=0)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=local_trial_end,
        )
        # Stripe's own trial_end (whatever Checkout was given) is a DIFFERENT
        # timestamp from the local one — the webhook must copy Stripe's value,
        # not assume it matches (trap 4).
        stripe_trial_end = local_trial_end + timedelta(hours=1)
        stripe_trial_end_ts = int(stripe_trial_end.timestamp())

        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created",
                status="trialing",
                trial_end=stripe_trial_end_ts,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert sub.stripe_subscription_id == "sub_1"
        assert sub.trial_end == datetime.fromtimestamp(
            stripe_trial_end_ts, tz=dt_timezone.utc
        )
        assert billing_access.is_active(_refetch(coach)) is True

        # The local clock passes before `updated` lands — must NOT lock the
        # coach out (trap 1): a Stripe trial never lapses on the local clock.
        CoachSubscription.objects.filter(coach=coach).update(
            trial_end=timezone.now() - timedelta(minutes=1)
        )
        fresh_coach = _refetch(coach)
        assert billing_access.is_active(fresh_coach) is True
        assert billing_access.is_over_limit(fresh_coach) is False
        assert billing_access.can_add_athlete(fresh_coach) is True

        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="active")
        )
        sub.refresh_from_db()
        assert sub.status == CoachSubscription.Status.ACTIVE

        # subscription_started fired exactly once — at created(trialing), not
        # again at updated(active) (#509).
        stripe_started = [
            e
            for e in _events(EventName.SUBSCRIPTION_STARTED)
            if e.props.get("subscription") == "sub_1"
        ]
        assert len(stripe_started) == 1


class TestWebhookTrialEndGuard:
    def test_created_trialing_without_a_trial_end_leaves_the_local_clock_unchanged(
        self,
    ):
        """No ``trial_end`` in the Stripe payload → the local marker is untouched.

        May already pass on main (``trial_end`` isn't written at all there) —
        that's the one allowed exception; it's still asserted as a guard.
        """
        coach = _coach_with_customer()
        local_trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=local_trial_end,
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.created", status="trialing")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.trial_end == local_trial_end


# ---------------------------------------------------------------------------
# webhooks — mirroring a scheduled cancel (#556, item 3)
#
# Verified in Stripe test mode (2026-09-19): a Customer Portal cancel of an
# active subscription sets ``cancel_at`` (equal to ``current_period_end``) and
# leaves ``cancel_at_period_end`` **false** — a boolean copied from
# ``cancel_at_period_end`` alone would miss every Portal cancel. The API's own
# ``cancel_at_period_end=True`` shape carries no ``cancel_at`` and falls back
# to ``current_period_end``.
# ---------------------------------------------------------------------------


class TestWebhookCancelAtMirror:
    def test_portal_shape_sets_cancel_at_from_the_field_itself(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        cancel_ts = 1900000000
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.updated",
                status="active",
                period_end=cancel_ts,
                cancel_at=cancel_ts,
                cancel_at_period_end=False,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.cancel_at == datetime.fromtimestamp(cancel_ts, tz=dt_timezone.utc)

    def test_api_shape_falls_back_to_current_period_end(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        period_end = 1900000000
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.updated",
                status="active",
                period_end=period_end,
                cancel_at_period_end=True,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.cancel_at == datetime.fromtimestamp(period_end, tz=dt_timezone.utc)

    def test_a_later_event_with_neither_field_clears_it(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
            cancel_at=timezone.now(),
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="active")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.cancel_at is None


class TestWebhookStaleEventProtectsAStripeTrial:
    """Trap 2: a stale event for another subscription id must not clobber a trial.

    ``LIVE_STRIPE_STATUSES`` alone doesn't cover ``trialing``.
    """

    def test_stale_active_update_for_an_old_id_does_not_touch_a_stripe_trial(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_new",
            trial_end=timezone.now() + timedelta(days=10),
        )
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.updated", sub_id="sub_old", status="active"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert sub.stripe_subscription_id == "sub_new"

    def test_stale_delete_for_an_old_id_does_not_touch_a_stripe_trial(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_new",
            trial_end=timezone.now() + timedelta(days=10),
        )
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.deleted", sub_id="sub_old", status="canceled"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING
        assert sub.stripe_subscription_id == "sub_new"


class TestInvoicePaymentFailedDuringAStripeTrial:
    def test_stripe_trial_moves_to_past_due(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=timezone.now() + timedelta(hours=1),
        )
        billing_webhooks.handle_event(_real_invoice_event("invoice.payment_failed"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE

    def test_local_no_card_trial_is_untouched_by_invoice_events(self):
        """A local trial has no ``stripe_subscription_id``.

        No invoice event can reference it, so it's never nudged.
        """
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=10),
        )
        billing_webhooks.handle_event(_real_invoice_event("invoice.payment_failed"))
        billing_webhooks.handle_event(_real_invoice_event("invoice.paid"))
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.TRIALING


# ---------------------------------------------------------------------------
# webhooks — a canceled subscription id is terminal (P1-A, adversarial review)
#
# Stripe never reactivates a canceled subscription (`canceled` and
# `incomplete_expired` are terminal), so a non-delete event for the SAME id
# as an already-CANCELED row is stale by definition — a late/retried
# `created`/`updated` must not reopen it.
# ---------------------------------------------------------------------------


class TestWebhookCanceledSubscriptionIdIsTerminal:
    def test_late_created_trialing_for_the_same_id_does_not_reopen(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        future_trial_end = int((timezone.now() + timedelta(days=10)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created",
                status="trialing",
                trial_end=future_trial_end,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED
        assert billing_access.is_active(_refetch(coach)) is False

    def test_late_updated_active_for_the_same_id_does_not_reopen(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="active")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED

    def test_a_new_id_still_takes_over_a_canceled_row(self):
        """Regression guard for the existing re-subscribe path.

        A different subscription id must still take over. May already pass
        before the fix (P1-A only ignores events for the SAME id as the
        canceled row).
        """
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created", sub_id="sub_2", status="active"
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_2"

    def test_ignored_events_backfill_exactly_one_started_and_cancelled(self):
        """Round 2 (#555 review): a CANCELED row with no prior recorded events.

        The mirror's own history can't tell "this subscription never really
        started" apart from "it started, but the ``deleted`` was delivered
        before the ``created``/``updated`` — so we never got the chance to
        record it". #509's ledger rule is "started once, cancelled at most
        once, whatever the delivery order", so the ignored events below must
        now backfill the missing pair instead of staying silent (round 1's
        pinned 0/0 outcome).
        """
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_1",
        )
        future_trial_end = int((timezone.now() + timedelta(days=10)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created",
                status="trialing",
                trial_end=future_trial_end,
            )
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="active")
        )
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1
        assert len(_events(EventName.SUBSCRIPTION_CANCELLED)) == 1


# ---------------------------------------------------------------------------
# webhooks — the canceled-id guard backfills a missed live pair (round 2)
#
# Stripe doesn't guarantee delivery order: a `deleted` can arrive BEFORE the
# `created`/`updated` it logically follows. When that happens the row goes
# CANCELED without ever recording `subscription_started` (nothing was
# `already_live` or previously recorded), and the P1-A guard would then
# silently swallow the late `created`/`updated` that proves the subscription
# really was live — losing both events forever. The guard must backfill them.
# ---------------------------------------------------------------------------


class TestWebhookCanceledIdGuardBackfillsAMissedLivePair:
    def test_deleted_before_created_backfills_the_missing_pair(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=10),
        )
        future_trial_end = int((timezone.now() + timedelta(days=10)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.deleted",
                status="canceled",
                trial_end=future_trial_end,
            )
        )
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created",
                status="trialing",
                trial_end=future_trial_end,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED
        assert billing_access.is_active(_refetch(coach)) is False
        started = _events(EventName.SUBSCRIPTION_STARTED)
        cancelled = _events(EventName.SUBSCRIPTION_CANCELLED)
        assert len(started) == 1
        assert len(cancelled) == 1
        # Reconstructed after the fact: the prior status and the cancel reason
        # aren't known here, so the rows say so rather than guess ("canceled"
        # would read as a returning subscriber).
        assert started[0].props["previous"] == ""
        assert started[0].props["backfilled"] is True
        assert cancelled[0].props["backfilled"] is True

    def test_a_duplicate_late_created_does_not_duplicate_the_backfill(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(days=10),
        )
        future_trial_end = int((timezone.now() + timedelta(days=10)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.deleted",
                status="canceled",
                trial_end=future_trial_end,
            )
        )
        for _ in range(2):
            billing_webhooks.handle_event(
                _real_sub_event(
                    "customer.subscription.created",
                    status="trialing",
                    trial_end=future_trial_end,
                )
            )
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1
        assert len(_events(EventName.SUBSCRIPTION_CANCELLED)) == 1

    def test_a_real_prior_start_is_not_double_recorded(self):
        """created(active) already recorded the real pair the normal way.

        A late retried updated for the same, now-canceled id must not add a
        second started/cancelled.
        """
        coach = _coach_with_customer()
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.created", status="active")
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.deleted", status="canceled")
        )
        billing_webhooks.handle_event(
            _real_sub_event("customer.subscription.updated", status="active")
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.CANCELED
        assert len(_events(EventName.SUBSCRIPTION_STARTED)) == 1
        assert len(_events(EventName.SUBSCRIPTION_CANCELLED)) == 1


# ---------------------------------------------------------------------------
# webhooks — a stale `trialing` event past its own trial_end (P1-B, review)
#
# Stripe moves a subscription out of `trialing` at `trial_end`; a late or
# retried `created`/`updated(trialing)` reporting a `trial_end` already in
# the past can't be describing the subscription's current state.
# ---------------------------------------------------------------------------


class TestWebhookStaleTrialingPastItsTrialEndIsIgnored:
    def test_late_trialing_for_an_active_row_does_not_revert_it(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_1",
        )
        past_trial_end = int((timezone.now() - timedelta(hours=1)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.created",
                status="trialing",
                trial_end=past_trial_end,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        state = presenters.billing_state(_refetch(coach))
        assert state["first_charge_at"] is None

    def test_late_trialing_for_a_past_due_row_does_not_revert_it(self):
        coach = _coach_with_customer()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
        )
        past_trial_end = int((timezone.now() - timedelta(hours=1)).timestamp())
        billing_webhooks.handle_event(
            _real_sub_event(
                "customer.subscription.updated",
                status="trialing",
                trial_end=past_trial_end,
            )
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.PAST_DUE
        assert billing_access.is_active(_refetch(coach)) is False


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
        # The "started" marker (adversarial review of #556) — what lets a
        # later ``?billing=success`` turn into the pending state.
        assert CHECKOUT_STARTED_SESSION_KEY in c.session

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
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any(
            "You already have a subscription. Manage it in Manage billing." in t
            for t in texts
        )

    def test_stripe_trial_coach_is_not_double_charged(self, settings):
        """A Stripe-backed trialing row is already a live subscription (#555)."""
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            stripe_subscription_id="sub_1",
            trial_end=timezone.now() + timedelta(days=10),
        )
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session"
        ) as create:
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("already have a subscription" in t for t in texts)

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


class TestSubscribeViewNeverOpensASecondSubscription:
    """Item 2 (#556): never open a second subscription.

    The local mirror can lag the webhook, so ``billing_subscribe`` also asks
    Stripe directly before opening a Checkout — a coach who just subscribed
    (or is mid-Checkout in another tab) must not double-subscribe even while
    the mirror still reads free/local-trial.
    """

    URL = "/meso/billing/subscribe/"

    def _coach_client(self, mirror_status=None, **sub_kwargs):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        if mirror_status is not None:
            CoachSubscriptionFactory(coach=coach, status=mirror_status, **sub_kwargs)
        c = Client()
        c.force_login(coach)
        return coach, c

    @pytest.mark.parametrize("mirror", ["none", "free", "local_trial"])
    @pytest.mark.parametrize(
        "status",
        ["trialing", "active", "past_due", "incomplete", "unpaid", "paused"],
    )
    def test_an_open_stripe_subscription_blocks_a_new_checkout(
        self, settings, mirror, status
    ):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        sub_kwargs = {}
        mirror_status = None
        if mirror == "free":
            mirror_status = CoachSubscription.Status.FREE
        elif mirror == "local_trial":
            mirror_status = CoachSubscription.Status.TRIALING
            sub_kwargs["trial_end"] = timezone.now() + timedelta(days=10)
        coach, c = self._coach_client(mirror_status, **sub_kwargs)
        with (
            mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list([status])),
            mock.patch(GATEWAY_CHECKOUT) as create,
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        # Plain ``meso:billing`` — no ``?billing=success`` round trip (adversarial
        # review of #556): Stripe already told us a subscription exists, so the
        # pending marker is set directly in the coach's session instead.
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any(
            "You already have a subscription. Manage it in Manage billing." in t
            for t in texts
        )
        assert CHECKOUT_PENDING_SESSION_KEY in c.session

    @pytest.mark.parametrize("statuses", [["canceled"], ["incomplete_expired"], []])
    def test_only_ended_or_no_subscriptions_let_checkout_proceed(
        self, settings, statuses
    ):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with (
            mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list(statuses)),
            mock.patch(
                GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
            ) as create,
        ):
            resp = c.post(self.URL)
        assert resp.url == "https://stripe/cs"
        create.assert_called_once()

    def test_subscription_list_called_with_the_expected_kwargs(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with (
            mock.patch(
                GATEWAY_SUB_LIST, return_value=_subscription_list([])
            ) as list_mock,
            mock.patch(
                GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
            ),
        ):
            c.post(self.URL)
        list_mock.assert_called_once_with(customer="cus_x", status="all", limit=100)

    def test_no_customer_id_skips_the_stripe_check(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        with (
            mock.patch(GATEWAY_SUB_LIST) as list_mock,
            mock.patch(
                GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
            ),
        ):
            resp = c.post(self.URL)
        list_mock.assert_not_called()
        assert resp.url == "https://stripe/cs"

    def test_subscription_list_raising_fails_closed(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with (
            mock.patch(
                GATEWAY_SUB_LIST, side_effect=stripe.error.APIConnectionError("boom")
            ),
            mock.patch(GATEWAY_CHECKOUT) as create,
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        # One accurate message (adversarial review of #556) — not the old
        # "nothing was charged", which this code can't actually promise.
        assert any(t == STRIPE_UNAVAILABLE_MESSAGE for t in texts)

    def test_resource_missing_customer_lets_checkout_proceed(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        err = stripe.error.InvalidRequestError(
            "No such customer", "customer", code="resource_missing"
        )
        with (
            mock.patch(GATEWAY_SUB_LIST, side_effect=err),
            mock.patch(
                GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
            ) as create,
        ):
            resp = c.post(self.URL)
        assert resp.url == "https://stripe/cs"
        create.assert_called_once()

    def test_other_invalid_request_error_fails_closed(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        err = stripe.error.InvalidRequestError("bad", "limit", code="other_code")
        with (
            mock.patch(GATEWAY_SUB_LIST, side_effect=err),
            mock.patch(GATEWAY_CHECKOUT) as create,
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/billing/"
        create.assert_not_called()


class TestSubscribeViewExpiresOpenCheckoutsBeforeCreatingANewOne:
    """Item 2 (#556): expire other open Checkouts before creating a new one.

    Right before opening a new Checkout, expire the customer's other open
    subscription Checkout Sessions — the list check alone can't stop an
    *older* Checkout tab that was open before the first subscription existed.
    """

    URL = "/meso/billing/subscribe/"

    def _coach_client(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        coach.stripe_customer_id = "cus_x"
        coach.save(update_fields=["stripe_customer_id"])
        c = Client()
        c.force_login(coach)
        return coach, c

    def test_open_subscription_session_is_expired_payment_session_is_not(
        self, settings
    ):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        sessions = _session_list([("cs_sub", "subscription"), ("cs_pay", "payment")])
        with (
            mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list([])),
            mock.patch(GATEWAY_SESSION_LIST, return_value=sessions),
            mock.patch(GATEWAY_SESSION_EXPIRE) as expire,
            mock.patch(
                GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
            ) as create,
        ):
            resp = c.post(self.URL)
        expire.assert_called_once_with("cs_sub")
        create.assert_called_once()
        assert resp.url == "https://stripe/cs"

    def test_expiry_runs_before_checkout_is_created(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        call_order = []
        with (
            mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list([])),
            mock.patch(
                GATEWAY_SESSION_LIST,
                side_effect=lambda **kw: (
                    call_order.append("list_sessions"),
                    _session_list([]),
                )[1],
            ),
            mock.patch(
                "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
                side_effect=lambda *a, **kw: (
                    call_order.append("checkout"),
                    mock.Mock(url="https://stripe/cs"),
                )[1],
            ),
        ):
            c.post(self.URL)
        assert call_order == ["list_sessions", "checkout"]

    def test_expire_raising_fails_closed(self, settings):
        """A session still genuinely open when ``expire`` fails still bounces.

        (The gateway's own retrieve-and-tolerate fallback for a session that
        merely raced closed is covered directly in ``TestExpireOpenSubscriptionCheckouts``.)
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        sessions = _session_list([("cs_sub", "subscription")])
        with (
            mock.patch(GATEWAY_SUB_LIST, return_value=_subscription_list([])),
            mock.patch(GATEWAY_SESSION_LIST, return_value=sessions),
            mock.patch(
                GATEWAY_SESSION_EXPIRE,
                side_effect=stripe.error.InvalidRequestError("gone", "id"),
            ),
            mock.patch(GATEWAY_SESSION_RETRIEVE, return_value=mock.Mock(status="open")),
            mock.patch(GATEWAY_CHECKOUT) as create,
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any(t == STRIPE_UNAVAILABLE_MESSAGE for t in texts)


class TestSubscribeViewCompedCoachBounces:
    """Item 6 (#556): a stale Subscribe POST from a comped coach is refused.

    A comped coach has no Subscribe button, so any Subscribe POST — with or
    without a stale ``first_charge`` marker — must be refused before it ever
    reaches Stripe.
    """

    URL = "/meso/billing/subscribe/"

    def _coach_client(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        CoachSubscriptionFactory(coach=coach, status=CoachSubscription.Status.COMPED)
        c = Client()
        c.force_login(coach)
        return coach, c

    @pytest.mark.parametrize("post_data", [{}, {"first_charge": "1234567890"}])
    def test_comped_coach_bounces_without_opening_checkout(self, settings, post_data):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with mock.patch(GATEWAY_CHECKOUT) as create:
            resp = c.post(self.URL, data=post_data)
        assert resp.status_code == 302
        assert resp.url == "/meso/billing/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("Your plan changed. Nothing was charged." in t for t in texts)
        assert not any("starts billing today" in t for t in texts)


class TestSubscribeViewDeferredCharge:
    """Checkout through the view carries the deferred-first-charge rule (#555)."""

    URL = "/meso/billing/subscribe/"
    BILLING_URL = "/meso/billing/"

    def _coach_client(self):
        coach = UserFactory()
        CoachProfileFactory(user=coach)
        c = Client()
        c.force_login(coach)
        return coach, c

    def test_ten_days_left_defers_the_first_charge(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            resp = c.post(self.URL)
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert kwargs["subscription_data"] == {"trial_end": int(trial_end.timestamp())}

        page = c.get(self.BILLING_URL)
        body = page.content.decode()
        expected_date = dateformat.format(trial_end, "M j")
        # The date is wrapped in a `<time>` (#555 P1-C — local-timezone rewrite).
        assert "you won't be charged until" in body
        assert f">{expected_date}</time>" in body
        # The hidden marker carries the promised unix timestamp, not a bare
        # "deferred" flag (#555 P2-1).
        expected_promise = str(int(trial_end.timestamp()))
        assert (
            f'<input type="hidden" name="first_charge" value="{expected_promise}">'
            in body
        )

    def test_forty_seven_hours_left_charges_today(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(hours=47),
        )
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            resp = c.post(self.URL)
        assert resp.status_code == 302
        kwargs = create.call_args.kwargs
        assert "subscription_data" not in kwargs

        page = c.get(self.BILLING_URL)
        body = page.content.decode()
        assert "in under 2 days. Subscribing starts billing today." in body
        assert 'name="first_charge"' not in body

    @pytest.mark.parametrize("setup", ["free", "lapsed_trial"])
    def test_gateway_receives_no_trial_end(self, settings, setup):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        if setup == "lapsed_trial":
            CoachSubscriptionFactory(
                coach=coach,
                status=CoachSubscription.Status.TRIALING,
                trial_end=timezone.now() - timedelta(days=1),
            )
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
            return_value=mock.Mock(url="https://stripe/checkout"),
        ) as create:
            c.post(self.URL)
        assert create.call_args.kwargs["trial_end"] is None

    def test_stripe_params_have_no_subscription_data_for_a_free_coach(self, settings):
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://stripe/cs")
        ) as create:
            c.post(self.URL)
        assert "subscription_data" not in create.call_args.kwargs

    def test_stale_promise_bounces_without_opening_checkout(self, settings):
        """The page promised a deferred charge but the trial has since dropped.

        Under 48h left, the stale POST must not open Checkout (#555).
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() + timedelta(hours=47),
        )
        with mock.patch(GATEWAY_CHECKOUT) as create:
            resp = c.post(self.URL, data={"first_charge": "deferred"})
        assert resp.status_code == 302
        assert resp.url == "/meso/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("subscribing now starts billing today" in t for t in texts)

    def test_promised_date_mismatch_bounces_without_opening_checkout(self, settings):
        """The trial_end moved since the page was rendered (#555 P2-1).

        The stale-page marker now carries the *promised* unix timestamp, not a
        bare "deferred" flag — so a trial_end that changed underneath the page
        (an admin edit, a Stripe event) is caught even while still deferrable.
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        trial_end = timezone.now() + timedelta(days=10)
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=trial_end,
        )
        stale_promised = str(int(trial_end.timestamp()) + 3600)
        with mock.patch(GATEWAY_CHECKOUT) as create:
            resp = c.post(self.URL, data={"first_charge": stale_promised})
        assert resp.status_code == 302
        assert resp.url == "/meso/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("trial end date changed" in t for t in texts)

    def test_canceled_coach_gets_a_neutral_stale_page_message(self, settings):
        """The row is no longer a local trial at all (#555 round 2 nit).

        E.g. the coach subscribed and canceled in another tab between page
        load and this POST — `deferred_first_charge` is None here too, but
        "your trial has less than 2 days left" would be wrong (there's no
        trial to speak of any more).
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_old",
        )
        with mock.patch(GATEWAY_CHECKOUT) as create:
            resp = c.post(self.URL, data={"first_charge": "1234567890"})
        assert resp.status_code == 302
        assert resp.url == "/meso/"
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("Your plan changed since this page loaded" in t for t in texts)
        assert not any("trial has less than 2 days left" in t for t in texts)

    def test_lapsed_trial_gets_the_neutral_stale_page_message(self, settings):
        """The trial already ended: "less than 2 days left" would be wrong too.

        Nothing sweeps a lapsed local trial back to free, so the row still
        reads TRIALING with a past clock.
        """
        settings.MESO_PRO_PRICE_ID = "price_pro_test"
        coach, c = self._coach_client()
        CoachSubscriptionFactory(
            coach=coach,
            status=CoachSubscription.Status.TRIALING,
            trial_end=timezone.now() - timedelta(days=2),
        )
        with mock.patch(GATEWAY_CHECKOUT) as create:
            resp = c.post(self.URL, data={"first_charge": "1234567890"})
        assert resp.status_code == 302
        create.assert_not_called()
        texts = [m.message for m in get_messages(resp.wsgi_request)]
        assert any("Your plan changed since this page loaded" in t for t in texts)
        assert not any("trial has less than 2 days left" in t for t in texts)


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
