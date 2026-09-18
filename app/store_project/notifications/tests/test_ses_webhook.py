"""SES → SNS event webhook (issue #507, part 1).

Production's SES configuration set ("Tracking") publishes every send,
delivery, open, click, bounce, and complaint event to an SNS topic, which
POSTs the notification to ``ScopedSESEventWebhookView`` (mounted at
``ses/events/`` and, for the legacy payload shape, ``ses/bounce/`` — see
``config.urls``). That view first checks the notification's ``TopicArn``
against ``settings.AWS_SES_EVENT_TOPIC_ARNS`` (see ``TestTopicGuard`` below),
then verifies the SNS envelope's signature, then fires one of
``django_ses.signals.{bounce,complaint,delivery,send,open,click}_received``.

This file exercises the webhook end to end, one behaviour at a time:

- A notification for a ``TopicArn`` that isn't allow-listed is rejected
  before the signature is ever checked and nothing is recorded
  (``TestTopicGuard``).
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
from django.db import OperationalError
from django.urls import reverse
from django_ses.models import BlacklistedEmail

from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail

pytestmark = pytest.mark.django_db

# Must match `settings.AWS_SES_EVENT_TOPIC_ARNS` in `config.settings.test`.
SNS_TOPIC_ARN = "arn:aws:sns:us-east-2:497780720908:EmailOpens"
FOREIGN_TOPIC_ARN = "arn:aws:sns:us-east-2:999999999999:SomeoneElsesTopic"


def sns_envelope(
    message: dict,
    *,
    sns_message_id="sns-msg-1",
    notif_type="Notification",
    topic_arn=SNS_TOPIC_ARN,
):
    """Wrap an SES event ``message`` dict in an SNS ``Notification`` envelope."""
    return {
        "Type": notif_type,
        "MessageId": sns_message_id,
        "TopicArn": topic_arn,
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


def open_message(
    *, message_id="ses-msg-open", recipient="athlete@example.com", tags=None
):
    return {
        "eventType": "Open",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "source": "Lance Goyke <lance@lancegoyke.com>",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {} if tags is None else tags,
        },
        "open": {
            "timestamp": "2026-09-18T19:10:59.633Z",
            "userAgent": (
                "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 "
                "(via ggpht.com GoogleImageProxy)"
            ),
            "ipAddress": "66.102.8.97",
            "isBotEvent": "Unlikely",
        },
    }


def click_message(*, message_id="ses-msg-click", recipient="athlete@example.com"):
    return {
        "eventType": "Click",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
        },
        "click": {
            "timestamp": "2026-09-18T19:11:05.000Z",
            "userAgent": "Mozilla/5.0",
            "ipAddress": "66.102.8.97",
            "link": "https://mastering.fitness/meso/me/",
            "linkTags": {},
        },
    }


def delivery_message(*, message_id="ses-msg-delivery", recipient="athlete@example.com"):
    return {
        "eventType": "Delivery",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
        },
        "delivery": {
            "timestamp": "2026-09-18T19:10:56.000Z",
            "processingTimeMillis": 500,
            "recipients": [recipient],
            "smtpResponse": "250 2.6.0 Message received",
            "reportingMTA": "a8-70.smtp-out.us-east-2.amazonses.com",
        },
    }


def send_message(*, message_id="ses-msg-send", recipient="athlete@example.com"):
    return {
        "eventType": "Send",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
        },
        "send": {},
    }


def reject_message(*, message_id="ses-msg-reject", recipient="athlete@example.com"):
    """A SES event type django-ses doesn't dispatch a signal for."""
    return {
        "eventType": "Reject",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
        },
        "reject": {"reason": "Bad content"},
    }


def legacy_bounce_message(
    *, message_id="ses-msg-legacy-bounce", recipient="legacy@example.com"
):
    """The old ``notificationType`` payload shape ``ses/bounce/`` used to see.

    ``ScopedSESEventWebhookView`` (like the base ``SESEventWebhookView`` it
    subclasses) dispatches on ``eventType`` first, falling back to
    ``notificationType`` — this is what SNS sent before the event webhook
    switched configuration sets to the newer ``eventType`` field.
    """
    return {
        "notificationType": "Bounce",
        "mail": {
            "timestamp": "2026-09-18T19:10:53.918Z",
            "source": "Lance Goyke <lance@lancegoyke.com>",
            "messageId": message_id,
            "destination": [recipient],
            "tags": {},
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
            "feedbackId": "feedback-legacy",
        },
    }


def post_notification(client, message, *, url_name="ses_events", **kwargs):
    envelope = sns_envelope(message, **kwargs)
    return client.post(
        reverse(url_name),
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


@mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
class TestEmailEventRecording:
    """Each event type becomes an ``EmailEvent`` row via ``notifications.ses_events``."""

    def test_open_event_records_user_agent_bot_flag_and_matched_kind(
        self, _verify, client
    ):
        sent = SentEmail.objects.create(
            ses_message_id="ses-msg-open",
            recipient="athlete@example.com",
            kind=EmailKind.BLOCK_DELIVERED,
        )

        response = post_notification(
            client, open_message(message_id="ses-msg-open"), sns_message_id="sns-open-1"
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-open-1")
        assert event.event_type == EmailEvent.EventType.OPEN
        assert event.recipient == "athlete@example.com"
        assert event.user_agent.startswith("Mozilla/5.0")
        assert event.is_bot == "Unlikely"
        assert event.sent_email == sent
        assert event.kind == EmailKind.BLOCK_DELIVERED

    def test_open_event_without_a_sent_email_match_falls_back_to_mail_tags(
        self, _verify, client
    ):
        response = post_notification(
            client,
            open_message(
                message_id="ses-msg-untracked", tags={"kind": ["coach_invite"]}
            ),
            sns_message_id="sns-open-2",
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-open-2")
        assert event.sent_email is None
        assert event.kind == EmailKind.COACH_INVITE

    def test_click_event_records_the_link(self, _verify, client):
        response = post_notification(
            client, click_message(), sns_message_id="sns-click-1"
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-click-1")
        assert event.event_type == EmailEvent.EventType.CLICK
        assert event.link == "https://mastering.fitness/meso/me/"

    def test_delivery_event_records_a_row_per_recipient(self, _verify, client):
        response = post_notification(
            client,
            delivery_message(recipient="athlete@example.com"),
            sns_message_id="sns-delivery-1",
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-delivery-1")
        assert event.event_type == EmailEvent.EventType.DELIVERY
        assert event.recipient == "athlete@example.com"

    def test_bounce_event_records_bounce_type_and_subtype(self, _verify, client):
        response = post_notification(
            client,
            bounce_message(recipient="bounced2@example.com"),
            sns_message_id="sns-bounce-1",
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-bounce-1")
        assert event.event_type == EmailEvent.EventType.BOUNCE
        assert event.bounce_type == "Permanent"
        assert event.bounce_subtype == "General"

    def test_complaint_event_records_a_row(self, _verify, client):
        response = post_notification(
            client,
            complaint_message(recipient="complainer2@example.com"),
            sns_message_id="sns-complaint-1",
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-complaint-1")
        assert event.event_type == EmailEvent.EventType.COMPLAINT
        assert event.recipient == "complainer2@example.com"

    def test_send_event_records_a_row(self, _verify, client):
        response = post_notification(
            client, send_message(), sns_message_id="sns-send-1"
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-send-1")
        assert event.event_type == EmailEvent.EventType.SEND

    def test_redelivering_the_same_notification_does_not_duplicate(
        self, _verify, client
    ):
        post_notification(client, open_message(), sns_message_id="sns-redeliver-1")
        post_notification(client, open_message(), sns_message_id="sns-redeliver-1")

        assert EmailEvent.objects.filter(sns_message_id="sns-redeliver-1").count() == 1

    def test_unknown_event_type_is_ignored(self, _verify, client):
        """Reject/DeliveryDelay/RenderingFailure: django-ses fires no signal for these."""
        response = post_notification(
            client, reject_message(), sns_message_id="sns-reject-1"
        )

        assert response.status_code == 200
        assert EmailEvent.objects.count() == 0


class TestWebhookRobustness:
    """The webhook must always answer 200 to SNS, or SNS retries for hours."""

    def test_bad_json_returns_400(self, client):
        response = client.post(
            reverse("ses_events"), data="not json", content_type="application/json"
        )

        assert response.status_code == 400

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=False)
    def test_unverified_signature_returns_400_and_records_nothing(
        self, _verify, client
    ):
        response = post_notification(
            client, open_message(), sns_message_id="sns-unverified"
        )

        assert response.status_code == 400
        assert EmailEvent.objects.count() == 0

    @mock.patch("django_ses.views.utils.confirm_sns_subscription")
    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_subscription_confirmation_confirms_and_returns_200(
        self, _verify, confirm, client
    ):
        envelope = sns_envelope({}, notif_type="SubscriptionConfirmation")
        envelope["SubscribeURL"] = "https://sns.us-east-2.amazonaws.com/confirm"

        response = client.post(
            reverse("ses_events"),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert response.status_code == 200
        confirm.assert_called_once()

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_a_receiver_exception_still_returns_200(self, _verify, client):
        """A non-database exception is swallowed and logged, not propagated.

        See ``TestTransientDatabaseErrors`` below for the one kind of
        exception (``DatabaseError``) that's allowed through.
        """
        with mock.patch.object(
            EmailEvent.objects, "get_or_create", side_effect=RuntimeError("boom")
        ):
            response = post_notification(
                client, open_message(), sns_message_id="sns-receiver-boom"
            )

        assert response.status_code == 200


class TestTransientDatabaseErrors:
    """A transient ``DatabaseError`` must propagate, not get swallowed.

    Postgres restarting mid-deploy, a dropped connection, and the like must
    make SNS see something other than a 200 so it retries. Redelivery is
    safe: ``EmailEvent`` rows are idempotent on ``(sns_message_id,
    recipient)`` via ``get_or_create``.
    """

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_database_error_propagates_and_records_nothing(self, _verify, client):
        with (
            mock.patch.object(
                EmailEvent.objects,
                "get_or_create",
                side_effect=OperationalError("connection to server was lost"),
            ),
            pytest.raises(OperationalError),
        ):
            post_notification(client, open_message(), sns_message_id="sns-db-error-1")

        assert EmailEvent.objects.count() == 0


class TestTopicGuard:
    """The topic allow-list is checked before the SNS signature.

    ``ScopedSESEventWebhookView`` rejects any notification whose ``TopicArn``
    isn't in ``settings.AWS_SES_EVENT_TOPIC_ARNS`` — an attacker who
    subscribes our URL to their own topic must never get a certificate
    fetch, let alone a confirmed subscription or a recorded event.
    """

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_foreign_topic_is_rejected_and_signature_never_checked(
        self, verify, client
    ):
        response = post_notification(
            client,
            bounce_message(recipient="attacker-controlled@example.com"),
            sns_message_id="sns-foreign-1",
            topic_arn=FOREIGN_TOPIC_ARN,
        )

        assert response.status_code == 400
        assert EmailEvent.objects.count() == 0
        assert not BlacklistedEmail.objects.filter(
            email="attacker-controlled@example.com"
        ).exists()
        verify.assert_not_called()

    @mock.patch("django_ses.views.utils.confirm_sns_subscription")
    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_subscription_confirmation_for_foreign_topic_is_rejected(
        self, verify, confirm, client
    ):
        envelope = sns_envelope({}, notif_type="SubscriptionConfirmation")
        envelope["TopicArn"] = FOREIGN_TOPIC_ARN
        envelope["SubscribeURL"] = "https://sns.us-east-2.amazonaws.com/confirm"

        response = client.post(
            reverse("ses_events"),
            data=json.dumps(envelope),
            content_type="application/json",
        )

        assert response.status_code == 400
        confirm.assert_not_called()
        verify.assert_not_called()

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_empty_allow_list_rejects_an_otherwise_valid_envelope(
        self, _verify, client, settings
    ):
        settings.AWS_SES_EVENT_TOPIC_ARNS = []

        response = post_notification(
            client, open_message(), sns_message_id="sns-no-allow-list"
        )

        assert response.status_code == 400
        assert EmailEvent.objects.count() == 0

    @mock.patch("django_ses.views.utils.verify_event_message", return_value=True)
    def test_legacy_bounce_endpoint_on_allow_listed_topic_records_a_row(
        self, _verify, client
    ):
        response = post_notification(
            client,
            legacy_bounce_message(recipient="legacy-bounced@example.com"),
            sns_message_id="sns-legacy-bounce-1",
            url_name="ses_bounce",
        )

        assert response.status_code == 200
        event = EmailEvent.objects.get(sns_message_id="sns-legacy-bounce-1")
        assert event.event_type == EmailEvent.EventType.BOUNCE
        assert event.recipient == "legacy-bounced@example.com"


class TestTopicGuardWithoutSignatureVerification:
    """The topic guard must run even when signature verification is off.

    ``AWS_SES_VERIFY_EVENT_SIGNATURES=False`` is a supported configuration
    (e.g. local testing). django-ses's own ``SESEventWebhookView.post()``
    only calls ``verify_event_message`` at all when that setting is true, so
    a guard that lives solely inside ``verify_event_message`` would never run
    in this mode. Nothing here patches
    ``django_ses.views.utils.verify_event_message`` — the unverified path
    runs for real, proving the guard doesn't depend on it.
    """

    def test_foreign_topic_is_rejected_without_signature_verification(
        self, client, settings
    ):
        settings.AWS_SES_VERIFY_EVENT_SIGNATURES = False

        response = post_notification(
            client,
            bounce_message(recipient="attacker-controlled@example.com"),
            sns_message_id="sns-foreign-unverified",
            topic_arn=FOREIGN_TOPIC_ARN,
        )

        assert response.status_code == 400
        assert EmailEvent.objects.count() == 0
        assert not BlacklistedEmail.objects.filter(
            email="attacker-controlled@example.com"
        ).exists()

    def test_allow_listed_topic_still_works_without_signature_verification(
        self, client, settings
    ):
        settings.AWS_SES_VERIFY_EVENT_SIGNATURES = False

        response = post_notification(
            client,
            bounce_message(recipient="bounced-unverified@example.com"),
            sns_message_id="sns-allow-listed-unverified",
        )

        assert response.status_code == 200
        assert EmailEvent.objects.filter(
            sns_message_id="sns-allow-listed-unverified"
        ).exists()
