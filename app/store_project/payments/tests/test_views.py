from unittest import mock

import pytest
import stripe
from django.contrib.messages import get_messages
from django.core import mail
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.test import Client
from django.test import RequestFactory

from store_project.payments import views
from store_project.products.models import Program
from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_login_before_purchase_view(program: Program):
    response = Client().get(f"/payments/login-to-purchase/program/{program.slug}/")
    messages = list(get_messages(response.wsgi_request))

    assert len(messages) == 1
    assert str(messages[0]) == "You must be logged in to purchase."
    assert response.status_code == 302
    assert response.url == f"/accounts/login/?next=/programs/{program.slug}/"
    assert isinstance(response, HttpResponseRedirect)


def test_stripe_config_view():
    response = Client().get("/payments/config/")
    assert response.status_code == 200
    assert isinstance(response, JsonResponse)
    assert b"publicKey" in response.content


def test_create_checkout_session_view(user: User, program: Program, rf: RequestFactory):
    request = rf.get(
        f"/payments/create-checkout-session/?productSlug={program.slug}&productType=program"  # noqa: E501
    )
    request.user = user
    response = views.create_checkout_session(request)
    assert response.status_code == 200
    assert isinstance(response, JsonResponse)
    assert b"sessionId" in response.content


def test_stripe_webhook_view_missing_signature(rf: RequestFactory):
    """A request with no Stripe-Signature header is rejected with 400, not a 500."""
    request = rf.get("/payments/webhook/")
    response = views.stripe_webhook(request)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# checkout.session.completed (#545) — a Meso Pro *subscription* Checkout
# Session also completes and lands here, and a `payment` session can arrive
# with no product metadata (or an unknown customer, or a stale product name).
# None of those should fall back to the "Test Program" test defaults.
#
# Real events are built with `stripe.Event.construct_from` so the view sees
# genuine `stripe.StripeObject` instances (no `.get()`, KeyError/AttributeError
# on a missing key) -- the same style as
# `meso/tests/test_billing_stripe.py::_real_sub_event`.
# ---------------------------------------------------------------------------


def _checkout_event(
    *,
    mode="payment",
    customer="cus_test",
    metadata=None,
    amount_total=1100,
    session_id="cs_test_123",
    subscription=None,
):
    obj = {
        "id": session_id,
        "object": "checkout.session",
        "mode": mode,
        "customer": customer,
        "amount_total": amount_total,
        "metadata": metadata if metadata is not None else {},
    }
    if subscription is not None:
        obj["subscription"] = subscription
    event = stripe.Event.construct_from(
        {
            "id": "evt_test",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": obj},
        },
        "sk_test",
    )
    assert type(event["data"]["object"]) is stripe.checkout.Session
    return event


def _post_webhook(event):
    with mock.patch(
        "store_project.payments.views.stripe.Webhook.construct_event",
        return_value=event,
    ):
        return Client().post(
            "/payments/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="t=1,v1=test",
        )


def test_stripe_webhook_ignores_subscription_mode_sessions(user: User):
    """A Meso Pro subscription Checkout Session completes here too (#545)."""
    user.stripe_customer_id = "cus_meso_pro"
    user.save(update_fields=["stripe_customer_id"])
    event = _checkout_event(
        mode="subscription",
        customer="cus_meso_pro",
        subscription="sub_meso_1",
    )

    response = _post_webhook(event)

    assert response.status_code == 200
    assert not user.user_permissions.exists()
    assert not Program.objects.filter(name="Test Program").exists()
    assert mail.outbox == []


def test_stripe_webhook_payment_session_without_metadata_grants_nothing(user: User):
    user.stripe_customer_id = "cus_no_metadata"
    user.save(update_fields=["stripe_customer_id"])
    event = _checkout_event(mode="payment", customer="cus_no_metadata", metadata={})

    response = _post_webhook(event)

    assert response.status_code == 200
    assert not user.user_permissions.exists()
    assert not Program.objects.filter(name="Test Program").exists()
    assert mail.outbox == []


def test_stripe_webhook_payment_session_grants_and_emails_for_known_product(
    user: User, program: Program
):
    user.stripe_customer_id = "cus_known_product"
    user.save(update_fields=["stripe_customer_id"])
    event = _checkout_event(
        mode="payment",
        customer="cus_known_product",
        metadata={
            "product_name": program.name,
            "product_type": "program",
            "product_slug": program.slug,
        },
    )

    response = _post_webhook(event)

    assert response.status_code == 200
    assert user.user_permissions.filter(name=f"Can view {program.name}").exists()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "Your order was successful!"
    assert mail.outbox[0].to == [user.email]


def test_stripe_webhook_payment_session_unknown_customer_creates_no_user(
    program: Program,
):
    user_count = User.objects.count()
    event = _checkout_event(
        mode="payment",
        customer="cus_does_not_exist",
        metadata={
            "product_name": program.name,
            "product_type": "program",
            "product_slug": program.slug,
        },
    )

    response = _post_webhook(event)

    assert response.status_code == 200
    assert User.objects.count() == user_count
    assert not User.objects.filter(email="lancegoyke@gmail.com").exists()
    assert mail.outbox == []


def test_stripe_webhook_payment_session_unknown_product_grants_nothing(user: User):
    user.stripe_customer_id = "cus_unknown_product"
    user.save(update_fields=["stripe_customer_id"])
    program_count = Program.objects.count()
    event = _checkout_event(
        mode="payment",
        customer="cus_unknown_product",
        metadata={
            "product_name": "Does Not Exist",
            "product_type": "program",
            "product_slug": "does-not-exist",
        },
    )

    response = _post_webhook(event)

    assert response.status_code == 200
    assert Program.objects.count() == program_count
    assert not user.user_permissions.exists()
    assert mail.outbox == []
