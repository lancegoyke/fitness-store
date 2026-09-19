"""``message_sent`` → ``SentEmail`` (issue #507, part 1).

``django_ses.SESBackend.send_messages`` fires ``django_ses.signals.
message_sent`` right after SES accepts a message, with SES's own
``MessageId`` already stamped onto ``message.extra_headers["message_id"]``.
It never fires under the locmem backend the test suite uses (that backend
doesn't touch django-ses at all), so these tests send the signal directly —
the same shape ``SESBackend`` sends it in.
"""

import pytest
from django.core.mail import EmailMessage
from django_ses.signals import message_sent

from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def fire_message_sent(*, to, subject="Hello", message_id="ses-msg-1", tag=None):
    """Build a message and fire ``message_sent`` for it, like ``SESBackend`` would."""
    headers = {}
    if message_id is not None:
        headers["message_id"] = message_id
    if tag is not None:
        headers["X-SES-MESSAGE-TAGS"] = f"kind={tag}"
    message = EmailMessage(subject, "body", "from@example.com", to, headers=headers)
    message_sent.send(sender=object(), message=message)
    return message


class TestRecordSentEmail:
    def test_creates_a_row_with_kind_recipient_and_matched_user(self):
        user = UserFactory(email="athlete@example.com")

        fire_message_sent(
            to=["Athlete@Example.com"],
            subject="Your block is ready",
            message_id="ses-msg-block",
            tag="block_delivered",
        )

        sent = SentEmail.objects.get(ses_message_id="ses-msg-block")
        assert sent.recipient == "athlete@example.com"
        assert sent.kind == EmailKind.BLOCK_DELIVERED
        assert sent.subject == "Your block is ready"
        assert sent.user == user

    def test_recipient_lookup_is_case_insensitive(self):
        UserFactory(email="mixedcase@example.com")

        fire_message_sent(to=["MixedCase@Example.com"], message_id="ses-msg-case")

        sent = SentEmail.objects.get(ses_message_id="ses-msg-case")
        assert sent.user is not None
        assert sent.user.email == "mixedcase@example.com"

    def test_no_matching_user_leaves_user_null(self):
        fire_message_sent(to=["stranger@example.com"], message_id="ses-msg-stranger")

        sent = SentEmail.objects.get(ses_message_id="ses-msg-stranger")
        assert sent.user is None

    def test_untagged_message_defaults_to_other(self):
        fire_message_sent(to=["a@example.com"], message_id="ses-msg-untagged")

        sent = SentEmail.objects.get(ses_message_id="ses-msg-untagged")
        assert sent.kind == EmailKind.OTHER

    def test_missing_message_id_is_skipped(self):
        fire_message_sent(to=["a@example.com"], message_id=None)

        assert SentEmail.objects.count() == 0

    def test_resending_the_same_message_id_does_not_duplicate(self):
        fire_message_sent(to=["a@example.com"], message_id="ses-msg-dup")
        fire_message_sent(to=["a@example.com"], message_id="ses-msg-dup")

        assert SentEmail.objects.filter(ses_message_id="ses-msg-dup").count() == 1
