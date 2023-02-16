import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.http.response import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse

import botocore
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
        "price": int_to_price(checkout_session["amount_total"]),
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
    try:
        send_mail(
            subject="Your order was successful!",
            message=msg_plain,
            html_message=msg_html,
            from_email=None,  # will default to settings.DEFAULT_FROM_EMAIL
            recipient_list=[user.email],
            fail_silently=False,  # raises smtplib.SMTPException
        )
    except botocore.exceptions.ClientError as e:
        print(f"Send email error: {e}")
        return HttpResponse(status=500)
    print(f"[payments.views.stripe_webhook] Email sent to {user.email}.")
    logger.info(f"Successful order: {user.email}")


def stripe_customer_get_or_create(user: User) -> stripe.Customer:
    """
    A customer might be in our Django database, but not in Stripe.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    if user.stripe_customer_id:
        try:
            stripe_customer = stripe.Customer.retrieve(id=user.stripe_customer_id)
        except stripe.error.InvalidRequestError:
            logger.info(f"Could not find Stripe Customer with ID={user.stripe_customer_id}. Creating now.")
            stripe_customer = stripe.Customer.create(
                id=user.stripe_customer_id,
                email=user.email
            )
    else:
        stripe_customer = stripe.Customer.create(email=user.email)
        user.stripe_customer_id = stripe_customer.id
        user.save(update_fields=["stripe_customer_id"])
        logger.info(f"New Stripe Customer with ID={user.stripe_customer_id}.")

    return stripe_customer


def stripe_price_get_or_create(product: Product) -> str:
    """
    Because sometimes the Django Postgres database is not synced with the Products
    and Prices in Stripe.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        price_object = stripe.Price.retrieve(product.stripe_price_id)
        try:
            # Price exists, now find the Product
            stripe.Product.retrieve(str(product.id))
        except stripe.error.InvalidRequestError:
            stripe.Product.create(
                id=str(product.id),
                name=product.name,
                description=product.description,
                type="good",
            )
    except stripe.error.InvalidRequestError:
        # Price does not exist, get Product then Price
        try:
            stripe.Product.retrieve(str(product.id))
        except stripe.error.InvalidRequestError:
            stripe.Product.create(
                id=str(product.id),
                name=product.name,
                description=product.description,
                type="good",
            )
        price_object = stripe.Price.create(
            currency="USD",
            unit_amount=f"{int(product.price*100)}",
            product=str(product.id),
        )

    return price_object.id
