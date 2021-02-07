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

from store_project.payments.utils import int_to_price, order_confirmation_email
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
        domain_url = settings.DOMAIN_URL + "payments/"
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
                line_items = [
                    {
                        # Use the Price already in Stripe
                        "price": product.stripe_price_id,
                        "quantity": 1,
                    },
                ]
            else:
                line_items = [
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
                ]

            if request.user.stripe_customer_id:
                try:
                    stripe_customer = stripe.Customer.retrieve(id=request.user.stripe_customer_id)
                except stripe.error.InvalidRequestError:
                    logger.info("Could not find Stripe Customer with ID={stripe_customer_id}. Creating now.")
                    stripe_customer = stripe.Customer.create(
                        id=request.user.stripe_customer_id,
                        email=request.user.email
                    )
            else:
                stripe_customer = stripe.Customer.create(email=request.user.email)
                request.user.stripe_customer_id = stripe_customer.id
                request.user.save(update_fields=["stripe_customer_id"])

            checkout_session = stripe.checkout.Session.create(
                customer=stripe_customer,
                client_reference_id=request.user.id,
                success_url=domain_url + "success/?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain_url + "cancellation/",
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
    print("[payments.views.stripe_webhook] BEGIN")
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

    # Handle the checkout.session.completed event
    if event["type"] == "checkout.session.completed":
        print("[payments.views.stripe_webhook] Payment was successful.")

        # set variables
        checkout_session = event.data.object
        user_email = checkout_session.customer_email  # CLI test: null
        metadata = checkout_session.metadata  # CLI test: {}
        if user_email:
            user = User.objects.get(email=user_email)
        else:
            user = UserFactory(
                username="lancegoyke", email="lancegoyke@gmail.com"
            )  # user for testing

        # get program name from Stripe Checkout Session metadata
        try:
            program_name = metadata["program_name"]
        except KeyError:
            # if metadata not supplied, we're testing
            program_name = "Test Program"

        try:
            program = Program.objects.get(name=program_name)
        except Program.DoesNotExist:
            # create new for testing
            program = Program.objects.create(
                name="Test Program",
                description="Test description.",
                slug="test-program",
                price=1100,
                author=User.objects.get(email="lance@lancegoyke.com"),
                duration=1,
                frequency=3,
            )
            test_category, created = Category.objects.get_or_create(
                name="Test Category"
            )
            program.categories.add(test_category)

        # give customer account permissions for purchased product
        try:
            permission = Permission.objects.get(name=f"Can view {program_name}")
        except Permission.DoesNotExist:
            permission = Permission.objects.create(
                codename=f"can_view_{slugify(program_name)}",
                name=f"Can view {program_name}",
                content_type=ContentType.objects.get_for_model(Program),
            )
        user.user_permissions.add(permission)
        print(
            f"[payments.views.stripe_webhook] Customer permissions set for {user.email}."
        )

        try:
            order_confirmation_email(checkout_session, program, user)

        except smtplib.SMTPException as e:
            logger.error("Could not email user's order.")
            logger.error(f"{e}")
            logger.error("Be sure to follow up with user")
            logger.error(f"- User = {user.email}")
            logger.error(f"- Program Name = {program_name}")
            return HttpResponse(status=500)

    return HttpResponse(status=200)


class SuccessView(TemplateView):
    template_name = "payments/success.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        stripe.api_key = settings.STRIPE_SECRET_KEY
        session_id = self.request.GET.get("session_id", "")
        line_items = stripe.checkout.Session.list_line_items(session_id)  # dict
        context["products"] = []
        context["amount"] = ""
        for product in line_items.data:
            context["products"].append(product.description)  # str
            # this is broken
            # not returning a product object, only it's name
        context["amount"] = int_to_price(product.amount_total)  # in USD with 2 decimals
        return context


class CancellationView(TemplateView):
    template_name = "payments/cancellation.html"
