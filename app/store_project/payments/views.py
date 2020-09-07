import os

from django.conf import settings
from django.http.response import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import TemplateView

import stripe


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
        try:
            # Create a new Checkout Session for the order
            # Other optional params include:
            #   [billing_address_collection] - to display billing address details on the page
            #   [customer] - if you have an existing Stripe Customer ID
            #   [payment_intent_data] - lets you capture the payment later
            #   [customer_email] - lets you prefill the email input in the form
            # For full details see https://stripe.com/docs/api/checkout/sessions/create

            # ?session_id={CHECKOUT_SESSION_ID} means the redirect will have the session ID set as a query param
            checkout_session = stripe.checkout.Session.create(
                # Set client_reference_id to identify the user making the purchase
                client_reference_id=request.user.id
                if request.user.is_authenticated
                else None,
                success_url=domain_url + "success?session_id={CHECKOUT_SESSION_ID}",
                cancel_url=domain_url + "cancelled/",
                payment_method_types=["card"],
                mode="payment",
                line_items=[
                    {
                        "name": "Program",
                        "quantity": 1,
                        "currency": "usd",
                        "amount": "1000",
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
        # Invalid payload
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
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
