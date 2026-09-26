"""Social-provider names populate the project's account identity (#622)."""

import pytest
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.models import SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.urls import reverse

from store_project.meso.factories import CoachProfileFactory
from store_project.users.factories import UserFactory
from store_project.users.forms import SignupForm

User = get_user_model()
pytestmark = pytest.mark.django_db


def _request(rf):
    request = rf.get("/")
    SessionMiddleware(lambda current: current).process_request(request)
    request.session.save()
    return request


def _sociallogin(request, provider_id, payload):
    provider = get_adapter().get_provider(request, provider_id)
    return provider.sociallogin_from_response(request, payload)


def test_social_account_adapter_is_configured():
    adapter = get_adapter()
    assert adapter.__class__.__module__ == "store_project.users.adapters"

    from store_project.users.adapters import SocialAccountAdapter

    assert isinstance(adapter, SocialAccountAdapter)


@pytest.mark.parametrize(
    "provider_id,payload",
    [
        (
            "google",
            {
                "id": "google-maya",
                "email": "maya.google@example.com",
                "email_verified": True,
                "given_name": "Maya",
                "family_name": "Okonkwo",
            },
        ),
        (
            "facebook",
            {
                "id": "facebook-maya",
                "email": "maya.facebook@example.com",
                "name": "Maya Okonkwo",
                "first_name": "Maya",
                "last_name": "Okonkwo",
            },
        ),
    ],
    ids=["google", "facebook"],
)
def test_provider_response_populates_and_persists_name(rf, provider_id, payload):
    request = _request(rf)
    sociallogin = _sociallogin(request, provider_id, payload)

    assert sociallogin.user.name == "Maya Okonkwo"
    sociallogin.save(request)
    assert User.objects.get(pk=sociallogin.user.pk).name == "Maya Okonkwo"


def test_existing_name_is_never_overwritten(rf):
    user = UserFactory(name="Typed Name")
    sociallogin = SocialLogin(user=user)

    get_adapter().populate_user(
        rf.get("/"),
        sociallogin,
        {
            "name": "Provider Name",
            "first_name": "Provider",
            "last_name": "Name",
        },
    )

    assert user.name == "Typed Name"


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"name": "  Maya\n\tOkonkwo  "}, "Maya Okonkwo"),
        ({}, ""),
        ({"first_name": "Maya"}, "Maya"),
    ],
    ids=["whitespace", "no-name", "first-name-only"],
)
def test_provider_name_variants(rf, data, expected):
    user = User(name="")
    sociallogin = SocialLogin(user=user)

    get_adapter().populate_user(rf.get("/"), sociallogin, data)

    assert user.name == expected


def test_provider_name_is_truncated_to_the_user_field_limit(rf):
    user = User(name="")
    sociallogin = SocialLogin(user=user)

    get_adapter().populate_user(rf.get("/"), sociallogin, {"name": "M" * 300})

    assert user.name == "M" * 255


def test_blank_signup_name_does_not_erase_provider_name(rf):
    user = UserFactory(name="From Provider")
    form = SignupForm()
    form.cleaned_data = {"name": ""}

    form.signup(rf.post("/accounts/signup/"), user)

    user.refresh_from_db()
    assert user.name == "From Provider"


def test_typed_signup_name_replaces_provider_name(rf):
    user = UserFactory(name="From Provider")
    form = SignupForm()
    form.cleaned_data = {"name": "  Typed\nName  "}

    form.signup(rf.post("/accounts/signup/"), user)

    user.refresh_from_db()
    assert user.name == "Typed Name"


def test_social_signup_coach_invite_uses_provider_name(
    client, rf, django_capture_on_commit_callbacks
):
    request = _request(rf)
    sociallogin = _sociallogin(
        request,
        "google",
        {
            "id": "google-inviting-coach",
            "email": "maya.social@example.com",
            "email_verified": True,
            "given_name": "Maya",
            "family_name": "Okonkwo",
        },
    )
    sociallogin.save(request)
    coach = sociallogin.user
    CoachProfileFactory(user=coach)
    client.force_login(coach)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("meso:coach_invite"),
            {"email": "new.athlete@example.com"},
        )

    assert response.status_code == 302
    assert "Maya Okonkwo" in mail.outbox[-1].subject
    assert "Maya Okonkwo" in mail.outbox[-1].body
    assert "maya.social" not in mail.outbox[-1].subject
    assert "maya.social" not in mail.outbox[-1].body
