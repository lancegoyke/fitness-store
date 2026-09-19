"""Issue #514: tag allauth's outbound mail with an EmailKind.

``DefaultAccountAdapter.render_mail`` builds every account email (signup
confirmation, password reset, notices) and hands it back before
``send_mail`` calls ``.send()`` on it -- the hook point our project
``AccountAdapter`` uses to ``tag_kind()`` the message, wired in via
``ACCOUNT_ADAPTER`` (``config.settings.base``).

``kind_for_template_prefix`` is unit-tested directly (it needs no request or
real mail templates); the adapter's actual ``render_mail`` override is
exercised through the real signup + password-reset views, which is also
where ``ACCOUNT_ADAPTER`` being wired up at all gets proven -- if it weren't,
these would tag nothing and fall back to the default adapter's plain
``EmailMessage``/``EmailMultiAlternatives`` (no ``extra_headers`` entry).
"""

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from store_project.notifications.models import EmailKind
from store_project.users.adapters import kind_for_template_prefix
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

TAG_HEADER = "X-SES-MESSAGE-TAGS"


class TestKindForTemplatePrefix:
    """Every prefix this project's allauth version (65.18) actually sends."""

    @pytest.mark.parametrize(
        "template_prefix,expected",
        [
            ("account/email/email_confirmation", EmailKind.ACCOUNT_CONFIRMATION),
            (
                "account/email/email_confirmation_signup",
                EmailKind.ACCOUNT_CONFIRMATION,
            ),
            ("account/email/email_confirm", EmailKind.ACCOUNT_CONFIRMATION),
            ("account/email/password_reset_key", EmailKind.PASSWORD_RESET),
            ("account/email/password_reset", EmailKind.PASSWORD_RESET),
            ("account/email/password_reset_code", EmailKind.PASSWORD_RESET),
            ("account/email/unknown_account", EmailKind.PASSWORD_RESET),
        ],
    )
    def test_mapped_prefixes(self, template_prefix, expected):
        assert kind_for_template_prefix(template_prefix) == expected

    @pytest.mark.parametrize(
        "template_prefix",
        [
            "account/email/password_changed",
            "account/email/email_changed",
            "account/email/account_already_exists",
            "account/email/login_code",
            "account/email/something_brand_new",
        ],
    )
    def test_unmapped_prefixes_fall_back_to_account_notice(self, template_prefix):
        assert kind_for_template_prefix(template_prefix) == EmailKind.ACCOUNT_NOTICE


class TestRealSendPaths:
    """The adapter is wired up (``ACCOUNT_ADAPTER``) and actually tags mail."""

    def test_signup_confirmation_is_tagged_account_confirmation(self, client):
        assert settings.ACCOUNT_ADAPTER == "store_project.users.adapters.AccountAdapter"

        client.post(
            reverse("account_signup"),
            {"email": "newuser@example.com", "password1": "a very unguessable pw 42"},
        )

        assert len(mail.outbox) == 1
        assert (
            mail.outbox[0].extra_headers[TAG_HEADER]
            == f"kind={EmailKind.ACCOUNT_CONFIRMATION.value}"
        )

    def test_password_reset_for_existing_user_is_tagged_password_reset(self, client):
        UserFactory(email="reset-me@example.com")

        client.post(
            reverse("account_reset_password"), {"email": "reset-me@example.com"}
        )

        assert len(mail.outbox) == 1
        assert (
            mail.outbox[0].extra_headers[TAG_HEADER]
            == f"kind={EmailKind.PASSWORD_RESET.value}"
        )

    def test_password_reset_for_unknown_email_is_still_tagged_password_reset(
        self, client
    ):
        client.post(
            reverse("account_reset_password"), {"email": "nobody-here@example.com"}
        )

        assert len(mail.outbox) == 1
        assert (
            mail.outbox[0].extra_headers[TAG_HEADER]
            == f"kind={EmailKind.PASSWORD_RESET.value}"
        )
