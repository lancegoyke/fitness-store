from http import HTTPStatus
from unittest import mock

import pytest
import requests
from django.conf import settings
from django.core import mail
from django.test import Client
from django.test import RequestFactory
from django.test import TestCase

from store_project.pages import turnstile
from store_project.pages.factories import PageFactory
from store_project.pages.views import HomePageView
from store_project.pages.views import SinglePageView

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

    def _post(self, **overrides):
        """POST a valid submission, token included, unless told otherwise."""
        data = {
            "subject": "Subject",
            "user_email": "email@example.com",
            "message": "This is a test message.",
            turnstile.TOKEN_FIELD: "a-token",
        }
        data.update(overrides)
        return self.client.post("/contact/", data=data)

    def test_get(self):
        response = self.client.get("/contact/")

        assert response.status_code == HTTPStatus.OK
        assert b"<h1>Contact</h1>" in response.content
        assert b"subject" in response.content
        assert b"user_email" in response.content
        assert b"message" in response.content

    def test_get_renders_the_turnstile_widget(self):
        response = self.client.get("/contact/")

        assert b"cf-turnstile" in response.content
        assert b"test-turnstile-site-key" in response.content
        assert b"challenges.cloudflare.com" in response.content
        # reCAPTCHA is gone, not merely unused.
        assert b"g-recaptcha" not in response.content
        assert b"google.com/recaptcha" not in response.content

    def test_post_success(self):
        response = self._post()

        assert response.status_code == HTTPStatus.OK

    def test_post_success_emails_the_owner_and_acknowledges_the_sender(self):
        response = self._post()

        assert len(mail.outbox) == 2
        to_owner, acknowledgement = mail.outbox
        assert to_owner.to == [settings.DEFAULT_FROM_EMAIL]
        assert to_owner.reply_to == ["email@example.com"]
        assert "This is a test message." in to_owner.body
        assert acknowledgement.to == ["email@example.com"]
        assert b"Your message is in our inbox" in response.content

    def test_the_acknowledgement_repeats_nothing_from_the_form(self):
        """The sender's own text must not be relayed to the address they chose."""
        self._post(subject="BUY CHEAP PILLS", message="visit http://spam.example now")

        acknowledgement = mail.outbox[1]
        assert "BUY CHEAP PILLS" not in acknowledgement.subject
        assert "BUY CHEAP PILLS" not in acknowledgement.body
        assert "spam.example" not in acknowledgement.body

    def test_post_rejected_by_turnstile_sends_nothing(self):
        reply = mock.Mock()
        reply.json.return_value = {
            "success": False,
            "error-codes": ["invalid-input-response"],
        }
        with mock.patch.object(turnstile.requests, "post", return_value=reply):
            response = self._post()

        assert mail.outbox == []
        assert b"bot check didn" in response.content

    def test_post_without_a_token_sends_nothing(self):
        response = self._post(**{turnstile.TOKEN_FIELD: ""})

        assert mail.outbox == []
        assert b"bot check didn" in response.content

    def test_post_when_verification_is_unavailable_sends_nothing(self):
        with mock.patch.object(
            turnstile.requests, "post", side_effect=requests.ConnectionError("boom")
        ):
            response = self._post()

        assert mail.outbox == []
        assert b"temporarily unavailable" in response.content

    def test_a_failed_acknowledgement_still_confirms_receipt(self):
        """The owner's copy is what counts; a bounced acknowledgement is not fatal."""
        with mock.patch(
            "store_project.notifications.emails.EmailMessage.send",
            side_effect=[1, OSError("mailbox rejected")],
        ):
            response = self._post()

        assert response.status_code == HTTPStatus.OK
        assert b"Your message is in our inbox" in response.content
        assert b"double-check it" in response.content

    def test_post_error(self):
        response = self._post(message="")

        assert response.status_code == HTTPStatus.OK
        assert b"<strong>Message:</strong> This field is required." in response.content
        assert mail.outbox == []

    def test_turnstile_settings_are_configured(self):
        assert settings.TURNSTILE_SITE_KEY
        assert settings.TURNSTILE_SECRET_KEY
        assert settings.TURNSTILE_ENDPOINT
        assert settings.TURNSTILE_ALLOWED_HOSTNAMES


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


class TimerViewTests(TestCase):
    def test_get(self):
        response = self.client.get("/timer/")

        assert response.status_code == HTTPStatus.OK
        self.assertTemplateUsed(response, "pages/timer.html")

    def test_renders_the_controls_timer_js_binds_to(self):
        # timer.js looks these up by id; a template rename would break it
        # silently in the browser.
        html = self.client.get("/timer/").content.decode()

        for element_id in ("mode", "rounds", "work", "rest", "interval", "prep"):
            assert f'id="{element_id}"' in html

    def test_offers_the_repeating_emom_type(self):
        html = self.client.get("/timer/").content.decode()

        assert '<option value="emom">' in html
        # The repeat cycle is configurable, not pinned to 60 seconds.
        assert 'id="interval"' in html
        assert 'data-mode="emom"' in html
