import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.mark.django_db
class TestPopulateUsernamesCommand:
    """Test the populate_usernames management command."""

    def test_command_populates_empty_usernames(self):
        """Test that the command populates empty usernames."""
        # Create user with placeholder username, then clear it
        user = User.objects.create_user(
            username="temp", email="testuser@example.com", password="testpass123"
        )

        # Clear username to simulate the problem state
        User.objects.filter(id=user.id).update(username="")

        # Run the command
        call_command("populate_usernames")

        # Refresh from database
        user.refresh_from_db()

        # Check username was populated
        assert user.username == "testuser"

    def test_command_handles_duplicates(self):
        """Test that the command handles duplicate username conflicts."""
        # Create existing user with username 'testuser'
        User.objects.create_user(
            username="testuser", email="existing@example.com", password="testpass123"
        )

        # Create user with placeholder username, then clear it
        user_empty = User.objects.create_user(
            username="temp", email="testuser@newdomain.com", password="testpass123"
        )
        User.objects.filter(id=user_empty.id).update(username="")

        # Run the command
        call_command("populate_usernames")

        # Refresh from database
        user_empty.refresh_from_db()

        # Check that conflicting username got incremented
        assert user_empty.username == "testuser1"

    def test_command_dry_run(self, capsys):
        """Test that dry run mode doesn't make changes."""
        # Create user with placeholder username, then clear it
        user = User.objects.create_user(
            username="temp", email="testuser@example.com", password="testpass123"
        )
        User.objects.filter(id=user.id).update(username="")

        # Run dry run
        call_command("populate_usernames", "--dry-run")

        # Check that user wasn't changed
        user.refresh_from_db()
        assert user.username == ""

        # Check that dry run output was printed
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "testuser@example.com -> testuser" in captured.out

    def test_command_skips_users_without_email(self, capsys):
        """Test that command skips users without email addresses."""
        # Create user without email - need to use direct database manipulation
        user = User.objects.create_user(
            username="temp", email="temp@example.com", password="testpass123"
        )
        # Clear both username and email to simulate the problem state
        User.objects.filter(id=user.id).update(username="", email="")

        # Run the command
        call_command("populate_usernames")

        # Check that user wasn't changed
        user.refresh_from_db()
        assert user.username == ""

        # Check that skip message was printed
        captured = capsys.readouterr()
        assert "Skipped" in captured.out

    def test_command_with_no_empty_usernames(self, capsys):
        """Test command behavior when no users have empty usernames."""
        # Create user with normal username
        User.objects.create_user(
            username="normaluser", email="test@example.com", password="testpass123"
        )

        # Run the command
        call_command("populate_usernames")

        # Check output
        captured = capsys.readouterr()
        assert "No users found with empty usernames" in captured.out
