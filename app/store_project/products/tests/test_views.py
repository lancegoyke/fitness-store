import pytest
from django.contrib.auth.models import AnonymousUser
from django.http.response import Http404
from django.shortcuts import render
from django.test import RequestFactory
from django.urls import reverse

from store_project.users.models import User
from store_project.products.models import Program
from store_project.products.views import (
    ProgramListView,
    ProgramDetailView,
)

pytestmark = pytest.mark.django_db


class TestProgramDetailView:
    def test_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = user

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data = f'data-program-slug="{program.slug}"'
        admin_link = reverse("admin:products_program_change", args=(program.id,))

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert purchase_button_id in response.rendered_content
        assert purchase_button_data in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = AnonymousUser()

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data = f'data-program-slug="{program.slug}"'

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert login_to_purchase_link in response.rendered_content
        assert purchase_button_id not in response.rendered_content
        assert purchase_button_data not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, program: Program, rf: RequestFactory
    ):
        request = rf.get(f"/programs/{program.slug}/")
        request.user = superuser

        response = ProgramDetailView.as_view()(request, slug=program.slug)

        login_to_purchase_link = f'href="/payments/login-to-purchase/{program.slug}/">'
        purchase_button_id = 'id="submitButton"'
        purchase_button_data = f'data-program-slug="{program.slug}"'
        admin_link = reverse("admin:products_program_change", args=(program.id,))

        assert response.status_code == 200
        assert "products/program_detail.html" in response.template_name
        assert response.context_data["content"]
        assert response.context_data["program"]
        assert purchase_button_id in response.rendered_content
        assert purchase_button_data in response.rendered_content
        assert login_to_purchase_link not in response.rendered_content
        assert admin_link in response.rendered_content


class TestProgramListView:
    def test_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get("/programs/")
        request.user = AnonymousUser()

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["program_list"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_not_authenticated(self, user: User, program: Program, rf: RequestFactory):
        request = rf.get("/programs/")
        request.user = user

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["program_list"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link not in response.rendered_content

    def test_super_authenticated(
        self, superuser: User, program: Program, rf: RequestFactory
    ):
        request = rf.get("/programs/")
        request.user = superuser

        response = ProgramListView.as_view()(request)

        admin_link = reverse("admin:products_program_changelist")

        assert response.status_code == 200
        assert "products/program_list.html" in response.template_name
        assert response.context_data["program_list"]
        assert program.name in response.rendered_content
        assert program.description in response.rendered_content
        assert program.author.name in response.rendered_content
        assert f'href="/programs/{program.slug}/"' in response.rendered_content
        assert admin_link in response.rendered_content
