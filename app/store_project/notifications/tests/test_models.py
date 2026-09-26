"""``SentEmail`` / ``EmailEvent`` model shape (issue #507, part 1).

These are the two tracking tables the rest of #507 (part 1's receivers, part
2's superuser dashboard) is built on:

- ``SentEmail`` — one row per message actually handed to SES
  (``django_ses.signals.message_sent``), keyed by SES's own ``MessageId``.
- ``EmailEvent`` — one row per SES event (send/delivery/open/click/bounce/
  complaint) that SNS later reports for a message, denormalising ``kind`` from
  the ``SentEmail`` it's matched to so a dashboard can roll up by kind without
  a join.

This file only pins down the schema (defaults, uniqueness, the
``(sns_message_id, recipient)`` idempotency key) — the receivers that
populate these rows from the live webhook are covered in
``test_ses_webhook.py`` and ``test_sent_email.py``.
"""

import pytest
from django.db import IntegrityError
from django.utils import timezone

from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


class TestSentEmail:
    def test_defaults(self):
        sent = SentEmail.objects.create(
            ses_message_id="ses-1",
            recipient="athlete@example.com",
            subject="Your new training block is ready",
        )

        assert sent.kind == EmailKind.OTHER
        assert sent.user is None
        assert sent.sent_at is not None

    def test_ses_message_id_is_unique(self):
        SentEmail.objects.create(ses_message_id="ses-dup", recipient="a@example.com")
        with pytest.raises(IntegrityError):
            SentEmail.objects.create(
                ses_message_id="ses-dup", recipient="b@example.com"
            )

    def test_user_set_null_on_delete(self):
        user = UserFactory(email="athlete@example.com")
        sent = SentEmail.objects.create(
            ses_message_id="ses-2", recipient=user.email, user=user
        )

        user.delete()
        sent.refresh_from_db()

        assert sent.user is None


class TestEmailEvent:
    def test_defaults(self):
        event = EmailEvent.objects.create(
            event_type=EmailEvent.EventType.OPEN,
            ses_message_id="ses-3",
            sns_message_id="sns-1",
            recipient="athlete@example.com",
            occurred_at=timezone.now(),
        )

        assert event.kind == EmailKind.OTHER
        assert event.sent_email is None
        assert event.raw == {}

    def test_unique_per_sns_message_and_recipient(self):
        EmailEvent.objects.create(
            event_type=EmailEvent.EventType.BOUNCE,
            ses_message_id="ses-4",
            sns_message_id="sns-dup",
            recipient="a@example.com",
            occurred_at=timezone.now(),
        )
        with pytest.raises(IntegrityError):
            EmailEvent.objects.create(
                event_type=EmailEvent.EventType.BOUNCE,
                ses_message_id="ses-4",
                sns_message_id="sns-dup",
                recipient="a@example.com",
                occurred_at=timezone.now(),
            )

    def test_same_sns_message_different_recipient_is_allowed(self):
        """One bounce notification naming two recipients → two rows."""
        EmailEvent.objects.create(
            event_type=EmailEvent.EventType.BOUNCE,
            ses_message_id="ses-5",
            sns_message_id="sns-multi",
            recipient="a@example.com",
            occurred_at=timezone.now(),
        )
        EmailEvent.objects.create(
            event_type=EmailEvent.EventType.BOUNCE,
            ses_message_id="ses-5",
            sns_message_id="sns-multi",
            recipient="b@example.com",
            occurred_at=timezone.now(),
        )

        assert EmailEvent.objects.filter(sns_message_id="sns-multi").count() == 2

    def test_sent_email_set_null_on_delete(self):
        sent = SentEmail.objects.create(
            ses_message_id="ses-6", recipient="a@example.com"
        )
        event = EmailEvent.objects.create(
            sent_email=sent,
            event_type=EmailEvent.EventType.DELIVERY,
            ses_message_id="ses-6",
            sns_message_id="sns-2",
            recipient="a@example.com",
            occurred_at=timezone.now(),
        )

        sent.delete()
        event.refresh_from_db()

        assert event.sent_email is None
