import pytest

from django.urls import reverse, resolve

from store_project.products.models import Program


pytestmark = pytest.mark.django_db


def test_login_to_purchase(program: Program):
    assert (
        reverse("payments:login_to_purchase", kwargs={"product_type": "program", "product_slug": program.slug})
        == f"/payments/login-to-purchase/program/{program.slug}/"
    )
    assert (
        resolve(f"/payments/login-to-purchase/program/{program.slug}/").view_name
        == "payments:login_to_purchase"
    )


def test_stripe_config():
    assert reverse("payments:stripe_config") == "/payments/config/"
    assert resolve("/payments/config/").view_name == "payments:stripe_config"


def test_stripe_create_checkout_session():
    assert (
        reverse("payments:stripe_create_checkout_session")
        == "/payments/create-checkout-session/"
    )
    assert (
        resolve("/payments/create-checkout-session/").view_name
        == "payments:stripe_create_checkout_session"
    )


def test_success():
    assert reverse("payments:success") == "/payments/success/"
    assert resolve("/payments/success/").view_name == "payments:success"


def test_webhook():
    assert reverse("payments:webhook") == "/payments/webhook/"
    assert resolve("/payments/webhook/").view_name == "payments:webhook"
