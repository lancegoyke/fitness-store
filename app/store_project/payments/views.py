import os

from django.conf import settings
from django.contrib import messages
from django.http.response import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import TemplateView

import stripe

from store_project.products.models import Program


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
def create_checkout_session(request):
    """
    A Checkout Session is the programmatic representation of what your
    customer sees when they’re redirected to the payment form.
    Src: https://stripe.com/docs/payments/checkout/accept-a-payment#create-a-checkout-session
    """
    if request.method == "GET":
        domain_url = settings.DOMAIN_URL + "payments/"
        stripe.api_key = settings.STRIPE_SECRET_KEY
        program = Program.objects.get(slug=request.GET.get("program-slug"))

        try:
            # Create a new Checkout Session for the order
            # Other optional params include:
            #   [billing_address_collection] - to display billing address details on the page
            #   [customer] - if you have an existing Stripe Customer ID
            #   [payment_intent_data] - lets you capture the payment later
            #   [customer_email] - lets you prefill the email input in the form
            # For full details see https://stripe.com/docs/api/checkout/sessions/create

            # ?session_id={CHECKOUT_SESSION_ID} means the redirect will have the session ID set as a query param
            print(f"[PRINT] {program.featured_image.url}")
            checkout_session = stripe.checkout.Session.create(
                customer_email=request.user.email,
                client_reference_id=request.user.id,
                success_url=domain_url + "success?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain_url + "cancellation/",
                payment_method_types=["card"],
                mode="payment",
                line_items=[
                    {
                        # "name": f"{program.name}",
                        # "quantity": 1,
                        # "currency": "usd",
                        # "amount": f"{int(program.price*100)}",
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": f"{int(program.price*100)}",
                            "product_data": {
                                "name": f"{program.name}",
                                # This needs to be disabled until media files are no longer local
                                # "images": [f"{program.featured_image.url}"],
                                "images": [
                                    "https://lancegoyke.com/wp-content/uploads/2020/07/adult-architecture-athlete-boardwalk-221210-676x483.jpg",
                                ],
                            },
                        },
                        "quantity": 1,
                    }
                ],
            )
            return JsonResponse({"sessionId": checkout_session["id"]})
        except Exception as e:
            return JsonResponse({"error": str(e)})


@csrf_exempt
def stripe_webhook(request):
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
        # TODO: run some custom code here
        # create an order in the database
        # give customer account permissions for purchased product
        # send customer a success email

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
            context["products"].append(product.description)  # return str
            amount = float(product.amount_total / 100)
            context["amount"] = f"{amount:.2f}"  # return in USD
        return context


class CancellationView(TemplateView):
    template_name = "payments/cancellation.html"
