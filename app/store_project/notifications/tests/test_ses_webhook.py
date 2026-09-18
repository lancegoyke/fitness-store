"""SES → SNS event webhook (issue #507, part 1).

Production's SES configuration set ("Tracking") publishes every send,
delivery, open, click, bounce, and complaint event to an SNS topic, which
POSTs the notification to ``django_ses.views.SESEventWebhookView`` (mounted at
``ses/events/`` — see ``config.urls``). That view verifies the SNS envelope's
signature, then fires one of ``django_ses.signals.{bounce,complaint,delivery,
send,open,click}_received``.

This file exercises the webhook end to end, one behaviour at a time:

- ``django_ses`` is installed everywhere (not just production), so its own
  built-in bounce/complaint handlers run in tests too: a permanent bounce or a
  complaint blacklists the recipient in ``django_ses.models.BlacklistedEmail``
  (the app's own signal handlers, connected by ``DjangoSESConfig.ready()``).
- Each event type is turned into a ``notifications.models.EmailEvent`` row by
  this app's own receivers (``notifications.ses_events``), with the fields
  particular to that event (user agent / bot flag for opens, the link for
  clicks, bounce type/subtype for bounces, ...).
- Redelivery of the *same* SNS notification (SNS retries on anything but a
  200) does not duplicate a row — idempotency is keyed on
  ``(sns_message_id, recipient)``.
- Unknown event types, bad JSON, and a failed signature are all handled
  without ever raising past the view (a 5xx would make SNS retry for hours).
- ``SubscriptionConfirmation`` triggers ``utils.confirm_sns_subscription``.
- A receiver that raises must not turn into a 5xx.

SNS signature verification is mocked throughout
(``django_ses.views.utils.verify_event_message``) — this suite is about our
handling of an already-verified message, not the cryptography.
"""

import json
from unittest import mock

import pytest
from django.urls import reverse
from django_ses.models import BlacklistedEmail

pytestmark = pytest.mark.django_db


def sns_envelope(
    message: dict, *, sns_message_id="sns-msg-1", notif_type="Notification"
):
    """Wrap an SES event ``message`` dict in an SNS ``Notification`` envelope."""
    return {
        "Type": notif_type,
        "MessageId": sns_message_id,
        "TopicArn": "arn:aws:sns:us-east-2:497780720908:EmailOpens",
        "Subject": "Amazon SES Email Event Notification",
        "Message": json.dumps(message),
        "Timestamp": "2026-09-18T19:10:59.633Z",
        "SignatureVersion": "1",
        "Signature": "fake-signature",
        "SigningCertURL": "https://sns.us-east-2.amazonaws.com/cert.pem",
        "UnsubscribeURL": "https://sns.us-east-2.amazonaws.com/unsubscribe",
    }


def bounce_message(*, message_id="ses-msg-bounce", recipient="bounced@example.com"):
    return {
        "eventType": "Bounce",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "source": "Lance Goyke <lance@lancegoyke.com>",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {"kind": ["block_delivered"]},
        },
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [
                {
                    "emailAddress": recipient,
                    "action": "failed",
                    "status": "5.1.1",
                    "diagnosticCode": "smtp; 550 5.1.1 unknown user",
                }
            ],
            "timestamp": "2026-09-18T19:11:00.000Z",
            "feedbackId": "feedback-1",
        },
    }


def complaint_message(
    *, message_id="ses-msg-complaint", recipient="complainer@example.com"
):
    return {
        "eventType": "Complaint",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "source": "Lance Goyke <lance@lancegoyke.com>",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
        },
        "complaint": {
            "complainedRecipients": [{"emailAddress": recipient}],
            "timestamp": "2026-09-18T19:11:00.000Z",
            "feedbackId": "feedback-2",
            "complaintFeedbackType": "abuse",
            "userAgent": "SomeMailClient/1.0",
        },
    }


def post_notification(client, message, **kwargs):
    envelope = sns_envelope(message, **kwargs)
    return client.post(
        reverse("ses_events"),
        data=json.dumps(envelope),
        content_type="application/json",
    )


@mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
class TestBlacklisting:
    """django-ses's own bounce/complaint handlers blacklist the recipient.

    These run purely from ``django_ses`` being installed + configured
    (``AWS_SES_ADD_BOUNCE_TO_BLACKLIST`` / ``AWS_SES_ADD_COMPLAINT_TO_BLACKLIST``
    in ``base.py``) — no code of ours is involved yet.
    """

    def test_permanent_bounce_blacklists_recipient(self, _verify, client):
        response = post_notification(
            client, bounce_message(recipient="bounced@example.com")
        )

        assert response.status_code == 200
        assert BlacklistedEmail.objects.filter(email="bounced@example.com").exists()

    def test_complaint_blacklists_recipient(self, _verify, client):
        response = post_notification(
            client, complaint_message(recipient="complainer@example.com")
        )

        assert response.status_code == 200
        assert BlacklistedEmail.objects.filter(email="complainer@example.com").exists()
