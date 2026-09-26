"""Roster nudge for coaches whose invites use an email-prefix fallback (#622)."""

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachProfileFactory
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def _roster(client, *, name, display_name=""):
    coach = UserFactory(
        name=name,
        email="unnamed.coach@example.com",
    )
    CoachProfileFactory(user=coach, display_name=display_name)
    client.force_login(coach)
    response = client.get(reverse("meso:roster"))
    assert response.status_code == 200
    return response.content.decode()


def test_unnamed_coach_sees_invite_signature_nudge(client):
    body = _roster(client, name="")

    assert "Invites are signed “unnamed.coach”." in body
    assert f'href="{reverse("meso:settings")}"' in body
    assert ">Add your name</a> to change that." in body


def test_account_name_hides_invite_signature_nudge(client):
    body = _roster(client, name="Maya Okonkwo")

    assert "Invites are signed" not in body


def test_coach_display_name_hides_invite_signature_nudge(client):
    body = _roster(client, name="", display_name="Coach Maya")

    assert "Invites are signed" not in body


def test_whitespace_account_name_shows_invite_signature_nudge(client):
    body = _roster(client, name=" \n\t ")

    assert "Invites are signed “unnamed.coach”." in body
