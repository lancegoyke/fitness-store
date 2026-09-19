from unittest import mock

import botocore.exceptions
import pytest
from django.core import mail
from django.http.response import HttpResponse

from store_project.payments.utils import int_to_price
from store_project.payments.utils import order_confirmation_email
from store_project.users.factories import UserFactory

TAG_HEADER = "X-SES-MESSAGE-TAGS"


def test_int_to_price():
    assert int_to_price(1000) == "10.00"


@pytest.mark.django_db
class TestOrderConfirmationEmail:
    """Issue #514: order confirmations must carry an ``EmailKind``.

    The dashboard needs it, and ``send_mail()`` can't do it -- it never
    exposes ``extra_headers``. ``order_confirmation_email`` now builds its
    own ``EmailMultiAlternatives`` and tags it via
    ``notifications.emails.tag_kind``.
    """

    def _send(self, user=None):
        user = user or UserFactory(email="buyer@example.com")
        product = type("Product", (), {"name": "Test Program"})()
        checkout_session = {"amount_total": 2500}
        order_confirmation_email(checkout_session, product, user)
        return user

    def test_tags_the_message_as_order_confirmation(self):
        self._send()

        assert len(mail.outbox) == 1
        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=order_confirmation"

    def test_sends_to_the_user_with_an_html_alternative(self):
        user = self._send()

        message = mail.outbox[0]
        assert message.to == [user.email]
        assert message.alternatives
        assert message.alternatives[0][1] == "text/html"

    def test_client_error_is_still_caught_and_returns_500(self):
        error = botocore.exceptions.ClientError(
            {"Error": {"Code": "Throttling", "Message": "slow down"}}, "SendRawEmail"
        )
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=error
        ):
            response = order_confirmation_email(
                {"amount_total": 2500},
                type("Product", (), {"name": "Test Program"})(),
                UserFactory(email="buyer@example.com"),
            )

        assert isinstance(response, HttpResponse)
        assert response.status_code == 500
