import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from store_project.users.factories import UserFactory
from store_project.users.models import User
from store_project.users.views import UserUpdateView, user_profile_view

pytestmark = pytest.mark.django_db


class TestUserUpdateView:
    def test_get_success_url(self, user: User, rf: RequestFactory):
        view = UserUpdateView()
        # request = rf.get("/fake-url/")
        # request.user = user

        # view.request = request

        assert view.get_success_url() == "/users/profile/"

    def test_get_object(self, user: User, rf: RequestFactory):
        view = UserUpdateView()
        request = rf.get("/fake-url/")
        request.user = user

        view.request = request

        assert view.get_object() == user


class TestUserProfileView:
    def test_authenticated(self, user: User, rf: RequestFactory):
        request = rf.get("/fake-url/")
        request.user = UserFactory()

        response = user_profile_view(request)

        assert response.status_code == 200

    def test_not_authenticated(self, user: User, rf: RequestFactory):
        request = rf.get("/fake-url/")
        request.user = AnonymousUser()  # type: ignore

        response = user_profile_view(request)

        assert response.status_code == 302
        assert response.url == "/accounts/login/?next=/fake-url/"

    def test_case_sensitivity(self, rf: RequestFactory):
        request = rf.get("/fake-url/")
        request.user = UserFactory(email="UserName@example.com")

        response = user_profile_view(request)

        assert response.status_code == 200
