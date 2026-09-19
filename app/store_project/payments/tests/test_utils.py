from unittest import mock

import botocore.exceptions
import pytest
from django.core import mail
from django.http.response import HttpResponse

from store_project.payments.utils import int_to_price
from store_project.payments.utils import order_confirmation_email
from store_project.payments.utils import stripe_customer_get_or_create
from store_project.users.factories import UserFactory
from store_project.users.models import User

TAG_HEADER = "X-SES-MESSAGE-TAGS"


def test_int_to_price():
    assert int_to_price(1000) == "10.00"


@pytest.mark.django_db
class TestStripeCustomerGetOrCreateWriteOnce:
    """A user's ``stripe_customer_id`` is written once and never replaced.

    This function is shared by the store's checkout and Meso's subscription
    Checkout, so two unrelated call sites (or two concurrent requests from
    the same call site) can race for the same user with no lock between
    them (adversarial review of #556, round 2, Fix A). Simulating the full
    concurrent-thread race isn't necessary here: the case that matters is a
    caller whose in-memory ``user`` still reads ``""`` because another
    writer already committed a real id to the database out from under it —
    exactly what a losing thread would see.
    """

    def test_another_writer_already_set_the_id_loser_reuses_it(self):
        user = UserFactory()
        assert user.stripe_customer_id == ""
        # Another writer (a concurrent store checkout, or another Subscribe
        # tab) already committed a customer id for this user; this call's
        # in-memory ``user`` doesn't know that yet.
        User.objects.filter(pk=user.pk).update(stripe_customer_id="cus_winner")

        with (
            mock.patch(
                "store_project.payments.utils.stripe.Customer.create",
                return_value=mock.Mock(id="cus_orphan"),
            ) as create,
            mock.patch(
                "store_project.payments.utils.stripe.Customer.retrieve",
                return_value=mock.Mock(id="cus_winner"),
            ) as retrieve,
        ):
            customer = stripe_customer_get_or_create(user)

        # A customer was created (the eventual orphan) — write-once means
        # accepting that cost rather than a lock, not avoiding the call.
        create.assert_called_once()
        # The database id is unchanged (still the winner's), never
        # overwritten by the orphan this call just created.
        user.refresh_from_db(fields=["stripe_customer_id"])
        assert user.stripe_customer_id == "cus_winner"
        # The returned (and in-memory) customer is the winner's, not the
        # orphan.
        retrieve.assert_called_once_with("cus_winner")
        assert customer.id == "cus_winner"


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
