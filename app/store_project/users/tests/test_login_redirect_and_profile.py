import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from store_project.meso.models import CoachAthlete
from store_project.meso.models import CoachProfile
from store_project.products.factories import BookFactory
from store_project.products.factories import ProgramFactory
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _login(client, user, *, next_url=None):
    url = reverse("account_login")
    if next_url:
        url = f"{url}?next={next_url}"
    return client.post(url, {"login": user.email, "password": "testpass123"})


@pytest.mark.parametrize(
    "role,expected",
    [
        ("coach", "/meso/"),
        ("athlete", "/meso/me/"),
        ("other", "/users/profile/"),
    ],
)
def test_login_redirects_by_meso_role(client, role, expected):
    user = UserFactory()
    if role == "coach":
        CoachProfile.objects.create(user=user)
    elif role == "athlete":
        CoachAthlete.objects.create(
            coach=UserFactory(), athlete=user, status=CoachAthlete.Status.ACTIVE
        )

    response = _login(client, user)

    assert response.status_code == 302
    assert response.url == expected


@pytest.mark.parametrize("role", ["coach", "athlete", "other"])
def test_explicit_next_beats_role_redirect(client, role):
    user = UserFactory()
    if role == "coach":
        CoachProfile.objects.create(user=user)
    elif role == "athlete":
        CoachAthlete.objects.create(
            coach=UserFactory(), athlete=user, status=CoachAthlete.Status.ACTIVE
        )

    response = _login(client, user, next_url=reverse("products:store"))

    assert response.status_code == 302
    assert response.url == reverse("products:store")


def test_profile_lists_only_public_products_user_can_view(client):
    user = UserFactory()
    owned_program = ProgramFactory(name="Owned program", slug="owned-program")
    ProgramFactory(name="Other program", slug="other-program")
    owned_book = BookFactory(name="Owned book", slug="owned-book")
    BookFactory(name="Other book", slug="other-book")
    user.user_permissions.add(
        Permission.objects.get(codename="can_view_owned-program"),
        Permission.objects.get(codename="can_view_owned-book"),
    )
    client.force_login(user)

    response = client.get(reverse("users:profile"))
    body = response.content.decode()

    assert owned_program in response.context["programs"]
    assert owned_book in response.context["books"]
    assert "Owned program" in body
    assert "Owned book" in body
    assert "Other program" not in body
    assert "Other book" not in body


def test_profile_empty_states_link_to_store(client):
    client.force_login(UserFactory())

    body = client.get(reverse("users:profile")).content.decode()

    assert "You don't own any programs yet." in body
    assert "You don't own any books yet." in body
    assert body.count("Browse the store") == 2
