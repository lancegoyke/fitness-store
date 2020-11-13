import pytest

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.http import HttpResponseRedirect, JsonResponse
from django.test import Client, RequestFactory

from store_project.payments import views
from store_project.products.models import Program
from store_project.users.factories import UserFactory
from store_project.users.models import User


pytestmark = pytest.mark.django_db


def test_login_before_purchase_view(program: Program):
    response = Client().get(f"/payments/login-to-purchase/{program.slug}/")
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


def test_create_checkout_session_view_error(program: Program):
    response = Client().get(
        f"/payments/create-checkout-session/?program-slug={program.slug}"
    )
    assert response.status_code == 200
    assert isinstance(response, JsonResponse)
    assert b"error" in response.content


def test_create_checkout_session_view(user: User, program: Program, rf: RequestFactory):
    request = rf.get(f"/payments/create-checkout-session/?program-slug={program.slug}")
    request.user = user
    response = views.create_checkout_session(request)
    assert response.status_code == 200
    assert isinstance(response, JsonResponse)
    assert b"sessionId" in response.content


def test_stripe_webhook_view(rf: RequestFactory):
    request = rf.get(f"/payments/webhook/")
    with pytest.raises(KeyError):
        response = views.stripe_webhook(request)
