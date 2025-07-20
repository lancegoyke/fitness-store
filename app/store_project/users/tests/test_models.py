import pytest

from store_project.users.models import User

pytestmark = pytest.mark.django_db


def test_user_get_absolute_url(user: User):
    assert user.get_absolute_url() == "/users/profile/"
