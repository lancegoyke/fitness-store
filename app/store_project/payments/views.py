import logging
import os
import smtplib

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.decorators import login_required
from django.http.response import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import TemplateView

import stripe

from store_project.payments.utils import (
    int_to_price, order_confirmation_email, stripe_customer_get_or_create,
    stripe_price_get_or_create,
)
from store_project.products.models import Book, Category, Program
from store_project.users.factories import UserFactory


User = get_user_model()
logger = logging.getLogger(__name__)


def login_before_purchase(request, product_slug):
    if request.method == "GET":
        messages.error(request, "You must be logged in to purchase.")
        return redirect(
            f"{settings.LOGIN_URL}?next={reverse('products:program_detail', args=[product_slug])}"
        )


@csrf_exempt
def stripe_config(request):
    if request.method == "GET":
        stripe_config = {"publicKey": settings.STRIPE_PUBLISHABLE_KEY}
        # turn this dict into JSON for JavaScript to use
        return JsonResponse(stripe_config, safe=False)


@csrf_exempt
@login_required
def create_checkout_session(request):
    """
    A Checkout Session is the programmatic representation of what your
    customer sees when they’re redirected to the payment form.
    Src: https://stripe.com/docs/payments/checkout/accept-a-payment#create-a-checkout-session
    """
    if request.method == "GET":
        domain_url = settings.DOMAIN_URL
        stripe.api_key = settings.STRIPE_SECRET_KEY
        product_type = request.GET.get("product-type")
        product_slug = request.GET.get("product-slug")
        if product_type == "program":
            product = Program.objects.get(slug=product_slug)
        elif product_type == "book":
            product = Book.objects.get(slug=product_slug)

        try:
            # Create a new Checkout Session for the order
            # For more, see https://stripe.com/docs/api/checkout/sessions/create
            line_items = []
            if product.stripe_price_id:
                line_items.append(
                    {
                        # Use the Price ID we already have
                        "price": stripe_price_get_or_create(product),
                        "quantity": 1,
                    },
                )
            else:
                line_items.append(
                    {
                        # Generate a new Price in Stripe
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": f"{int(product.price*100)}",
                            "product_data": {
                                "name": f"{product.name}",
                            },
                        },
                        "quantity": 1,
                    },
                )

            stripe_customer = stripe_customer_get_or_create(request.user)

            checkout_session = stripe.checkout.Session.create(
                customer=stripe_customer,
                client_reference_id=str(request.user.id),
                success_url=domain_url + "payments/success/?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain_url + product.get_absolute_url()[1:],
                payment_method_types=["card"],
                mode="payment",
                line_items=line_items,
                allow_promotion_codes=True,
                metadata={
                    "product_name": product.name,
                    "product_type": product_type,
                    "product_slug": product_slug,
                    # add other pertinent links to download, etc...
                },
            )
            return JsonResponse({"sessionId": checkout_session["id"]})
        except Exception as e:
            return JsonResponse({"error": str(e)})


@csrf_exempt
def stripe_webhook(request):
    print("[payments.views.stripe_webhook] BEGIN HANDLING WEBHOOKS")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    endpoint_secret = os.environ.get("STRIPE_ENDPOINT_SECRET")
    payload = request.body
    signature_header = request.META["HTTP_STRIPE_SIGNATURE"]
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, signature_header, endpoint_secret
        )
    except ValueError as e:
        print("ERROR: Invalid payload")
        print(f"ERROR: {e}")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        print("ERROR: Invalid signature")
        print(f"ERROR: {e}")
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        print("[payments.views.stripe_webhook] Payment was successful.")

        checkout_session = event.data.object
        metadata = checkout_session.metadata  # CLI test: {}
        try:
            user = User.objects.get(stripe_customer_id=checkout_session["customer"])
            # Current bug: if user changes email address in Stripe, it's not
            # changed in Django. So we're finding User object with
            # `stripe_customer_id` instead.
        except User.DoesNotExist:
            user = UserFactory(
                username="lancegoyke", email="lancegoyke@gmail.com"
            )  # user for testing

        print(f"[payments.views.stripe_webhook] User = {user}")

        try:
            product_name = metadata["product_name"]
        except KeyError:
            # if metadata not supplied, we're testing
            product_name = "Test Program"

        try:
            product_type = metadata["product_type"]
        except KeyError:
            # if metadata not supplied, we're testing
            product_type = "program"

        # THE PROBLEM IS HERE IT SEEMS
        if product_type == "program":
            try:
                product = Program.objects.get(name=product_name)
            except Program.DoesNotExist:
                # create new Program for testing
                product = Program.objects.create(
                    name="Test Program",
                    description="Test description.",
                    slug="test-program",
                    price=1100,
                    author=User.objects.filter(email="lance@lancegoyke.com").first(),
                    duration=1,
                    frequency=3,
                )
                test_category, created = Category.objects.get_or_create(
                    name="Test Category"
                )
                product.categories.add(test_category)
        elif product_type == "book":
            product = Book.objects.get(name=product_name)

        print(f"[payments.views.stripe_webhook] Product = {product_name}")

        # give customer account permissions for purchased product
        try:
            permission = Permission.objects.get(name=f"Can view {product_name}")
        except Permission.DoesNotExist:
            permission = Permission.objects.create(
                codename=f"can_view_{slugify(product_name)}",
                name=f"Can view {product_name}",
                content_type=ContentType.objects.get_for_model(product.__class__),
            )
        user.user_permissions.add(permission)
        print(
            f'[payments.views.stripe_webhook] Permission "{permission.name}" given to {user.email}'
        )

        try:
            order_confirmation_email(checkout_session, product, user)
        except smtplib.SMTPException as e:
            logger.error(f"{e}")
            logger.error("Could not email order of {product_name} to {user.email}.")
            return HttpResponse(status=500)

        print("[payments.views.stripe_webhook] Successful payment webhook handled properly.")
    return HttpResponse(status=200)


class SuccessView(TemplateView):
    template_name = "payments/success.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        stripe.api_key = settings.STRIPE_SECRET_KEY
        session_id = self.request.GET.get("session_id", "")
        line_items = stripe.checkout.Session.list_line_items(session_id)  # dict
        context["products"] = []
        amount = 0
        for product in line_items.data:
            context["products"].append(product.description)  # str of title
            amount += product.amount_total
        context["amount"] = int_to_price(product.amount_total)  # in USD with 2 decimals
        return context
