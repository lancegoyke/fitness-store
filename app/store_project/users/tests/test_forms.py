import pytest
from django.contrib.auth import get_user_model

from store_project.users.factories import UserFactory
from store_project.users.forms import UserCreationForm

User = get_user_model()
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

    def test_auto_generate_username_from_email(self):
        """Test that username is auto-generated from email when not provided."""
        form = UserCreationForm(
            {
                "email": "testuser@example.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "testuser"
        assert user.email == "testuser@example.com"

    def test_auto_generate_username_handles_duplicates(self):
        """Test that duplicate usernames are handled by appending numbers."""
        # Create a user with username "testuser"
        User.objects.create_user(username="testuser", email="existing@example.com")

        # Try to create another user with email that would generate same username
        form = UserCreationForm(
            {
                "email": "testuser@newdomain.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "testuser1"
        assert user.email == "testuser@newdomain.com"

    def test_auto_generate_username_handles_multiple_duplicates(self):
        """Test that multiple duplicate usernames are handled correctly."""
        # Create users with usernames "testuser" and "testuser1"
        User.objects.create_user(username="testuser", email="user1@example.com")
        User.objects.create_user(username="testuser1", email="user2@example.com")

        # Try to create another user with email that would generate same base username
        form = UserCreationForm(
            {
                "email": "testuser@thirddomain.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "testuser2"
        assert user.email == "testuser@thirddomain.com"

    def test_explicit_username_takes_precedence(self):
        """Test that explicitly provided username is used instead of auto-generation."""
        form = UserCreationForm(
            {
                "username": "customuser",
                "email": "testuser@example.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "customuser"
        assert user.email == "testuser@example.com"

    def test_empty_username_field_is_allowed(self):
        """Test that empty username field doesn't cause validation errors."""
        form = UserCreationForm(
            {
                "username": "",
                "email": "testuser@example.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "testuser"

    def test_whitespace_only_username_is_treated_as_empty(self):
        """Test that whitespace-only username is treated as empty."""
        form = UserCreationForm(
            {
                "username": "   ",
                "email": "testuser@example.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        )

        assert form.is_valid()
        user = form.save()
        assert user.username == "testuser"
