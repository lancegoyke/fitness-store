"""S6 — billing, Phase 2: Stripe (Checkout + Portal + webhook + seat sync).

Phase 1 shipped the pure-backend spine (the ``CoachSubscription`` mirror, the
local no-card trial, the ``comped`` status, the gating accessor) with **no Stripe
and nothing enforced**. Phase 2 makes a coach able to actually *pay*:

- ``billing/stripe_gateway.py`` — thin, mockable wrappers over the ``stripe`` SDK:
  a subscription-mode Checkout Session, a hosted Customer Portal Session, and a
  best-effort seat-quantity sync;
- ``billing/webhooks.py`` — a clean, idempotent handler that materializes /
  updates a coach's ``CoachSubscription`` from the subscription + invoice events
  (the messy ``payments`` products webhook is left untouched);
- ``billing/seats.py`` — the best-effort inline seat sync, deferred to
  ``transaction.on_commit`` and never allowed to break the relationship change;
- the ``billing_subscribe`` / ``billing_portal`` / ``billing_webhook`` views;
- the daily ``reconcile_seats`` qcluster sweep (management command + task +
  registered schedule) that recomputes each paid coach's active seat count and
  corrects any Stripe drift — the correctness backstop behind the inline sync.

Stripe is mocked throughout (no network). See ``docs/meso/billing-plan.md``.
"""

from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client
from django_q.models import Schedule

from store_project.meso.billing import seats as billing_seats
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
GATEWAY_SUB_MODIFY = (
    "store_project.meso.billing.stripe_gateway.stripe.Subscription.modify"
)


def _paid_coach(*, quantity=1, status=CoachSubscription.Status.ACTIVE):
    """A coach who has actually subscribed (has Stripe ids) — a billable row."""
    sub = CoachSubscriptionFactory(
        status=status,
        stripe_subscription_id="sub_123",
        stripe_item_id="si_123",
        quantity=quantity,
    )
    return sub.coach, sub


# ---------------------------------------------------------------------------
# stripe_gateway — Checkout
# ---------------------------------------------------------------------------


class TestCheckoutSession:
    def test_creates_a_subscription_session_with_the_seat_price(self, settings):
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        coach = UserFactory()
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
        # Quantity = the coach's active seat count (2 active links here).
        assert kwargs["line_items"] == [{"price": "price_seat_test", "quantity": 2}]

    def test_quantity_floors_at_one_seat(self, settings):
        """A licensed subscription bills at least one seat (Stripe rejects 0)."""
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        coach = UserFactory()  # zero active links
        with mock.patch(
            GATEWAY_CHECKOUT, return_value=mock.Mock(url="https://x")
        ) as create:
            stripe_gateway.create_subscription_checkout_session(
                coach, success_url="https://x/ok", cancel_url="https://x/no"
            )
        assert create.call_args.kwargs["line_items"][0]["quantity"] == 1


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
# stripe_gateway — seat-quantity sync
# ---------------------------------------------------------------------------


class TestSeatSync:
    def test_pushes_new_quantity_for_a_paid_coach(self):
        coach, sub = _paid_coach(quantity=1)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is True
        modify.assert_called_once()
        args, kwargs = modify.call_args
        assert args[0] == "sub_123"
        assert kwargs["items"] == [{"id": "si_123", "quantity": 3}]
        sub.refresh_from_db()
        assert sub.quantity == 3

    def test_noop_when_already_in_sync(self):
        coach, sub = _paid_coach(quantity=1)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is False
        modify.assert_not_called()

    def test_noop_for_a_coach_without_a_stripe_subscription(self):
        sub = CoachSubscriptionFactory(status=CoachSubscription.Status.TRIALING)
        CoachAthleteFactory(coach=sub.coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(sub.coach)
        assert changed is False
        modify.assert_not_called()

    def test_noop_for_a_coach_with_no_subscription_row(self):
        coach = UserFactory()
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is False
        modify.assert_not_called()

    def test_noop_for_a_canceled_subscription(self):
        """A canceled mirror keeps its (dead) Stripe ids — never modify it."""
        coach, _ = _paid_coach(quantity=1, status=CoachSubscription.Status.CANCELED)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is False
        modify.assert_not_called()

    def test_syncs_a_past_due_subscription(self):
        coach, sub = _paid_coach(quantity=1, status=CoachSubscription.Status.PAST_DUE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is True
        modify.assert_called_once()

    def test_quantity_floors_at_one(self):
        """A paid coach who drops to zero active athletes still bills one seat."""
        coach, sub = _paid_coach(quantity=2)  # cached 2, now 0 active links
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            changed = stripe_gateway.sync_seat_quantity(coach)
        assert changed is True
        assert modify.call_args.kwargs["items"] == [{"id": "si_123", "quantity": 1}]
        sub.refresh_from_db()
        assert sub.quantity == 1


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
    quantity=2,
    period_end=1900000000,
):
    return {
        "type": type_,
        "data": {
            "object": {
                "id": sub_id,
                "customer": customer,
                "status": status,
                "current_period_end": period_end,
                "items": {"data": [{"id": item_id, "quantity": quantity}]},
            }
        },
    }


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


class TestWebhookHandler:
    def test_subscription_created_materializes_an_active_subscription(self):
        coach = _coach_with_customer()
        billing_webhooks.handle_event(
            _sub_event("customer.subscription.created", quantity=3)
        )
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.status == CoachSubscription.Status.ACTIVE
        assert sub.stripe_subscription_id == "sub_1"
        assert sub.stripe_item_id == "si_1"
        assert sub.quantity == 3
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
        event = _sub_event("customer.subscription.updated", quantity=4)
        billing_webhooks.handle_event(event)
        billing_webhooks.handle_event(event)
        assert CoachSubscription.objects.filter(coach=coach).count() == 1
        sub = CoachSubscription.objects.get(coach=coach)
        assert sub.quantity == 4

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
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        coach, c = self._coach_client()
        with mock.patch(
            "store_project.meso.views.billing_gateway.create_subscription_checkout_session",
            return_value=mock.Mock(url="https://stripe/checkout"),
        ):
            resp = c.post(self.URL)
        assert resp.status_code == 302
        assert resp.url == "https://stripe/checkout"

    def test_unconfigured_price_redirects_gracefully(self, settings):
        settings.MESO_SEAT_PRICE_ID = ""
        coach, c = self._coach_client()
        resp = c.post(self.URL)
        # No 500 — bounced back to the roster with a message.
        assert resp.status_code == 302
        assert resp.url == "/meso/"

    def test_get_is_rejected(self, settings):
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
        coach, c = self._coach_client()
        resp = c.get(self.URL)
        assert resp.status_code == 405

    def test_already_subscribed_coach_is_not_double_charged(self, settings):
        """A coach with a live Stripe subscription is bounced, not sent to a new Checkout."""
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
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
        settings.MESO_SEAT_PRICE_ID = "price_seat_test"
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


# ---------------------------------------------------------------------------
# inline best-effort seat sync (deferred to on_commit)
# ---------------------------------------------------------------------------


class TestSeatSyncHook:
    def test_schedule_seat_sync_calls_gateway_on_commit(
        self, django_capture_on_commit_callbacks
    ):
        coach = UserFactory()
        with mock.patch(
            "store_project.meso.billing.seats.stripe_gateway.sync_seat_quantity"
        ) as sync:
            with django_capture_on_commit_callbacks(execute=True):
                billing_seats.schedule_seat_sync(coach)
        sync.assert_called_once_with(coach)

    def test_a_gateway_failure_is_swallowed(self, django_capture_on_commit_callbacks):
        coach = UserFactory()
        with mock.patch(
            "store_project.meso.billing.seats.stripe_gateway.sync_seat_quantity",
            side_effect=RuntimeError("stripe down"),
        ):
            # Must not raise — the daily reconcile sweep is the backstop.
            with django_capture_on_commit_callbacks(execute=True):
                billing_seats.schedule_seat_sync(coach)

    def test_relationship_end_triggers_a_seat_sync(
        self, django_capture_on_commit_callbacks
    ):
        coach, _ = _paid_coach(quantity=2)
        link = CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        c = Client()
        c.force_login(coach)
        with mock.patch(
            "store_project.meso.billing.seats.stripe_gateway.sync_seat_quantity"
        ) as sync:
            with django_capture_on_commit_callbacks(execute=True):
                resp = c.post(f"/meso/relationship/{link.token}/end/")
        assert resp.status_code == 302
        sync.assert_called_once_with(coach)

    def test_invite_accept_triggers_a_seat_sync(
        self, django_capture_on_commit_callbacks
    ):
        coach, _ = _paid_coach(quantity=1)
        athlete = UserFactory()
        link = CoachAthlete.invite(coach=coach, athlete=athlete)
        c = Client()
        c.force_login(athlete)
        with mock.patch(
            "store_project.meso.billing.seats.stripe_gateway.sync_seat_quantity"
        ) as sync:
            with django_capture_on_commit_callbacks(execute=True):
                resp = c.post(f"/meso/invite/{link.token}/accept/")
        assert resp.status_code == 302
        sync.assert_called_once_with(coach)


# ---------------------------------------------------------------------------
# reconcile_seats — the daily sweep
# ---------------------------------------------------------------------------


class TestReconcileSeatsCommand:
    def test_corrects_drift_for_a_paid_coach(self):
        coach, sub = _paid_coach(quantity=1)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            call_command("meso_reconcile_seats")
        modify.assert_called_once()
        sub.refresh_from_db()
        assert sub.quantity == 2

    def test_skips_coaches_in_sync(self):
        coach, sub = _paid_coach(quantity=1)
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            call_command("meso_reconcile_seats")
        modify.assert_not_called()

    def test_skips_free_and_canceled_coaches(self):
        # Free coach with no Stripe sub.
        free = CoachSubscriptionFactory(status=CoachSubscription.Status.FREE)
        CoachAthleteFactory(coach=free.coach, status=CoachAthlete.Status.ACTIVE)
        # Canceled coach who still has a (now-dead) Stripe sub id.
        CoachSubscriptionFactory(
            status=CoachSubscription.Status.CANCELED,
            stripe_subscription_id="sub_dead",
            stripe_item_id="si_dead",
            quantity=5,
        )
        with mock.patch(GATEWAY_SUB_MODIFY) as modify:
            call_command("meso_reconcile_seats")
        modify.assert_not_called()

    def test_one_coachs_stripe_error_does_not_stop_the_sweep(self):
        bad, _ = _paid_coach(quantity=1)
        bad_sub = bad.coach_subscription
        bad_sub.stripe_subscription_id = "sub_bad"
        bad_sub.save(update_fields=["stripe_subscription_id"])
        CoachAthleteFactory(coach=bad, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=bad, status=CoachAthlete.Status.ACTIVE)
        good = UserFactory()
        CoachSubscriptionFactory(
            coach=good,
            status=CoachSubscription.Status.ACTIVE,
            stripe_subscription_id="sub_good",
            stripe_item_id="si_good",
            quantity=1,
        )
        CoachAthleteFactory(coach=good, status=CoachAthlete.Status.ACTIVE)
        CoachAthleteFactory(coach=good, status=CoachAthlete.Status.ACTIVE)

        def modify(sub_id, *a, **k):
            if sub_id == "sub_bad":
                raise RuntimeError("stripe boom")

        with mock.patch(GATEWAY_SUB_MODIFY, side_effect=modify):
            # Must not raise even though one coach errors.
            call_command("meso_reconcile_seats")
        good.coach_subscription.refresh_from_db()
        assert good.coach_subscription.quantity == 2


class TestReconcileScheduleRegistration:
    def test_reconcile_schedule_registered_daily(self):
        sched = Schedule.objects.get(name="meso-reconcile-seats")
        assert sched.func == "store_project.meso.tasks.reconcile_seats"
        assert sched.schedule_type == Schedule.DAILY

    def test_task_wrapper_is_importable(self):
        from store_project.meso import tasks

        assert callable(tasks.reconcile_seats)

    def test_task_wrapper_runs_the_command(self):
        from store_project.meso import tasks

        with mock.patch(GATEWAY_SUB_MODIFY):
            # Smoke: the wrapper drives the command without error.
            tasks.reconcile_seats()
