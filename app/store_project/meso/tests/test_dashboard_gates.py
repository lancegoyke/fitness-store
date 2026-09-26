import pytest
from django.conf import settings
from django.shortcuts import resolve_url
from django.urls import reverse

from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "url_name",
    [
        "meso:usage_dashboard",
        "meso:tour_funnel",
        "meso:product_analytics",
        "notifications:email_dashboard",
    ],
)
def test_dashboard_is_superuser_only(client, url_name):
    url = reverse(url_name)

    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"].startswith(resolve_url(settings.LOGIN_URL))

    client.force_login(UserFactory(is_staff=False))
    assert client.get(url).status_code == 403

    client.force_login(UserFactory(is_staff=True, is_superuser=False))
    assert client.get(url).status_code == 403

    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    assert client.get(url).status_code == 200
