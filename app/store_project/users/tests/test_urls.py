import pytest

from django.urls import resolve, reverse

from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_profile():
    assert reverse("users:profile") == "/users/profile/"
    assert resolve(f"/users/profile/").view_name == "users:profile"


def test_update():
    assert reverse("users:update") == "/users/update/"
    assert resolve("/users/update/").view_name == "users:update"
