import pytest
from store_project.users.factories import UserFactory
from store_project.users.forms import UserCreationForm

pytestmark = pytest.mark.django_db


class TestUserCreationForm:
    def test_clean_fields(self):
        # A user with proto_user params does not exist yet.
        proto_user = UserFactory.build()

        form = UserCreationForm(
            {
                "email": proto_user.email,
                "username": proto_user.username,
                "password1": proto_user.password,
                "password2": proto_user.password,
            }
        )

        assert form.is_valid()
        assert form.clean_email() == proto_user.email
        assert form.clean_username() == proto_user.username

        # Creating a user
        form.save()

        # The user with proto_user params already exists,
        # hence cannot be created.
        form = UserCreationForm(
            {
                "email": proto_user.email,
                "username": proto_user.username,
                "password1": proto_user.password,
                "password2": proto_user.password,
            }
        )

        assert not form.is_valid()
        assert len(form.errors) == 2
        assert "username" in form.errors
        assert "email" in form.errors
