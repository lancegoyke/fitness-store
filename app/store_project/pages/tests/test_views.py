import os
from http import HTTPStatus

import pytest
from django.test import Client, RequestFactory, TestCase
from store_project.pages.factories import PageFactory
from store_project.pages.views import HomePageView, SinglePageView

pytestmark = pytest.mark.django_db


def test_home_page_view(rf: RequestFactory):
    request = rf.get("/")

    response = HomePageView.as_view()(request)

    assert response.status_code == 200
    assert "pages/home.html" in response.template_name


def test_single_page_view(rf: RequestFactory):
    page = PageFactory(content="### Markdown Title")
    request = rf.get(f"/{page.slug}/")

    response = SinglePageView.as_view()(request, slug=page.slug)

    assert response.status_code == 200
    assert "pages/single.html" in response.template_name
    assert "<h3>Markdown Title</h3>" in response.rendered_content
    assert f"<h1>{page.title}</h1>" in response.rendered_content


class ContactViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_get(self):
        response = self.client.get("/contact/")

        assert response.status_code == HTTPStatus.OK
        assert b"<h1>Contact</h1>" in response.content
        assert b"subject" in response.content
        assert b"user_email" in response.content
        assert b"message" in response.content

    def test_post_success(self):
        response = self.client.post(
            "/contact/",
            data={
                "subject": "Subject",
                "user_email": "email@example.com",
                "message": "This is a test message.",
            },
        )
        assert response.status_code == HTTPStatus.OK

    def test_post_error(self):
        response = self.client.post(
            "/contact/",
            data={
                "subject": "Subject",
                "user_email": "email@example.com",
                "message": "",
            },
        )
        assert response.status_code == HTTPStatus.OK
        assert b"<strong>Message:</strong> This field is required." in response.content

    def test_env_vars_set(self):
        assert os.environ.get("G_RECAPTCHA_SITE_KEY")
        assert os.environ.get("G_RECAPTCHA_SECRET_KEY")
        assert os.environ.get("G_RECAPTCHA_ENDPOINT")


class RobotsTxtTests(TestCase):
    def test_get(self):
        response = self.client.get("/robots.txt")

        assert response.status_code == 200
        assert response["content-type"] == "text/plain"
        lines = response.content.decode().splitlines()
        assert lines[0] == "User-Agent: *"

    def test_post_disallowed(self):
        response = self.client.post("/robots.txt")

        assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED
