import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

import stripe

from store_project.products.models import Product


User = get_user_model()
logger = logging.getLogger(__name__)


def int_to_price(price: int) -> str:
    """Takes an int representing price in cents and returns a string
    representing a dollar amount with two decimal places."""
    return f"{float(price / 100):.2f}"


def order_confirmation_email(
    checkout_session: stripe.checkout.Session, product: Product, user: User
):
    context = {
        "product": product.name,
        "price": int_to_price(checkout_session.amount_total),
        "current_site": Site.objects.get_current(),
        "user": user,
        "account_url": reverse("users:profile"),
    }
    msg_plain = render_to_string(
        "payments/email/order_confirmation.txt",
        context,
    )
    msg_html = render_to_string(
        "payments/email/order_confirmation.html",
        context,
    )
    send_mail(
        subject="Your order was successful!",
        message=msg_plain,
        html_message=msg_html,
        from_email=None,  # will default to settings.DEFAULT_FROM_EMAIL
        recipient_list=[user.email],
        fail_silently=False,  # raises smtplib.SMTPException
    )
    print(f"[payments.views.stripe_webhook] Email sent to {user.email}.")
    logger.info(f"Successful order: {user.email}")


def stripe_price_get_or_create(product: Product) -> str:
    """
    Because sometimes the Django Postgres database is not synced with the Products
    and Prices in Stripe.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        price_object = stripe.Price.retrieve(product.stripe_price_id)
    except stripe.error.InvalidRequestError:
        price_object = stripe.Price.create(
            id=product.stripe_price_id,
            currency="USD",
            unit_amount=f"{int(product.price*100)}",
            product=product.id,
        )

    return price_object.id
