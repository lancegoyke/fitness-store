import pytest

from django.contrib.auth.models import AnonymousUser
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory

from store_project.payments import views
from store_project.products.models import Program
from store_project.users.factories import UserFactory
from store_project.users.models import User


pytestmark = pytest.mark.django_db


def test_login_before_purchase_anonymous_view(program: Program, rf: RequestFactory):
    request = rf.get(f"/payments/login-to-purchase/{program.slug}/")
    request.user = AnonymousUser()
    request._messages = []

    response = Client().get(f"/payments/login-to-purchase/{program.slug}/")
    messages = list(get_messages(response.wsgi_request))

    assert response.status_code == 302
    assert len(messages) == 1
    assert str(messages[0]) == "You must be logged in to purchase."
