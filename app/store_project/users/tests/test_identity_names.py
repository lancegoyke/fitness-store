import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from store_project.users import display
from store_project.users.factories import UserFactory

User = get_user_model()
pytestmark = pytest.mark.django_db


def test_profile_links_name_editor_and_update_cannot_change_email(client):
    user = UserFactory(name="", email="original@example.com")
    client.force_login(user)
    profile = client.get(reverse("users:profile")).content.decode()
    assert "Name: Not set" in profile
    assert reverse("users:update") in profile

    response = client.post(
        reverse("users:update"),
        {"name": "  Jordan\n Ellis ", "email": "attacker@example.com"},
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.name == "Jordan Ellis"
    assert user.email == "original@example.com"


@pytest.mark.parametrize("name", ["Maya Okonkwo", ""])
def test_signup_name_is_optional_and_persists(client, name):
    email = "named@example.com" if name else "unnamed@example.com"
    response = client.post(
        reverse("account_signup"),
        {
            "name": name,
            "email": email,
            "password1": "a very unguessable pw 42",
        },
    )
    assert response.status_code == 302
    assert User.objects.get(email=email).name == name


def test_account_user_display_prefers_name_then_full_email():
    named = UserFactory(name="Maya Okonkwo", email="maya@example.com")
    unnamed = UserFactory(name="", email="full.address@example.com")
    assert display.get_display_name(named) == "Maya Okonkwo"
    assert display.get_display_name(unnamed) == "full.address@example.com"
