import logging

import botocore
import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.mail import EmailMultiAlternatives
from django.http.response import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse

from store_project.notifications.emails import tag_kind
from store_project.notifications.models import EmailKind
from store_project.products.models import Product

User = get_user_model()
logger = logging.getLogger(__name__)


def int_to_price(price: int) -> str:
    """Turns int of cents into string of dollars and cents."""
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
    message = EmailMultiAlternatives(
        subject="Your order was successful!",
        body=msg_plain,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.ORDER_CONFIRMATION)
    try:
        message.send(fail_silently=False)  # raises smtplib.SMTPException
    except botocore.exceptions.ClientError as e:
        print(f"Send email error: {e}")
        return HttpResponse(status=500)
    print(f"[payments.views.stripe_webhook] Email sent to {user.email}.")
    logger.info(f"Successful order: {user.email}")


def stripe_customer_get_or_create(user: User) -> stripe.Customer:
    """A customer might be in our Django database, but not in Stripe.

    INVARIANT: a user's ``stripe_customer_id`` is written once and never
    replaced. Every Stripe object looked up later — a subscription, the
    Portal, the Meso billing webhook's customer→coach mapping
    (``billing/webhooks.py:_coach_for_customer``) — is keyed by it, so
    silently overwriting it would make an existing Stripe object invisible
    to this app forever.

    This function is shared by the store's one-time-purchase Checkout
    (``payments/views.py:create_checkout_session``) and Meso's subscription
    Checkout (``meso/billing/stripe_gateway.py``), so two unrelated call
    sites can race for the SAME user with no lock between them — e.g. a
    store purchase in one tab and a Meso Subscribe in another, or two
    concurrent Meso Subscribe tabs. When both see an empty
    ``stripe_customer_id`` in memory, both create a Stripe customer; the DB
    write below is a conditional (write-once) ``UPDATE ... WHERE
    stripe_customer_id = ''``, so only the writer that commits FIRST keeps
    its customer attached to the user. The loser's own newly-created
    customer becomes an unused orphan in Stripe — harmless, since nothing
    is ever attached to it — and the loser re-reads and returns the
    winner's customer instead.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    if user.stripe_customer_id:
        try:
            stripe_customer = stripe.Customer.retrieve(user.stripe_customer_id)
        except stripe.error.InvalidRequestError:
            logger.info(
                f"Could not find Stripe Customer with ID={user.stripe_customer_id}. Creating now."  # noqa: E501
            )
            stripe_customer = stripe.Customer.create(
                id=user.stripe_customer_id, email=user.email
            )
        return stripe_customer

    stripe_customer = stripe.Customer.create(email=user.email)
    updated = User.objects.filter(pk=user.pk, stripe_customer_id="").update(
        stripe_customer_id=stripe_customer.id
    )
    if not updated:
        # Someone else won the race and already wrote a (different) customer
        # id for this user between our read and our write above. Discard the
        # orphan we just created in Stripe (nothing will ever reference it)
        # and use the winner's customer instead.
        user.refresh_from_db(fields=["stripe_customer_id"])
        logger.info(
            "Stripe customer race for user %s: another writer already set "
            "%s; discarding the orphaned customer %s this call created.",
            user.pk,
            user.stripe_customer_id,
            stripe_customer.id,
        )
        return stripe.Customer.retrieve(user.stripe_customer_id)

    user.stripe_customer_id = stripe_customer.id
    logger.info(f"New Stripe Customer with ID={user.stripe_customer_id}.")
    return stripe_customer


def stripe_price_get_or_create(product: Product) -> str:
    """Helps sync Django database with the Products and Prices in Stripe."""
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
            )
        price_object = stripe.Price.create(
            currency="USD",
            unit_amount=f"{int(product.price * 100)}",
            product=str(product.id),
        )

    return price_object.id
