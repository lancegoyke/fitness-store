import logging

from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.mail import send_mail
from django.template.loader import render_to_string

import stripe

from store_project.products.models import Product, Program


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
        "product_url": product.program_file.url if product.program_file else None,
        "current_site": Site.objects.get_current(),
        "user": user,
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