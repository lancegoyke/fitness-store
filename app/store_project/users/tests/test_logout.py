import pytest
from django.test import Client

from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_logout_on_get():
    User.objects.create_user(
        username="logoutuser",
        email="logout@example.com",
        password="testpass123",
    )
    client = Client()
    client.login(email="logout@example.com", password="testpass123")
    response = client.get("/accounts/logout/")
    assert response.status_code == 302
    assert response.url == "/accounts/login/"
