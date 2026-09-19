"""E-mail delivery tracking (issue #507, part 1).

SES publishes a send/delivery/open/click/bounce/complaint event for every
message sent through the "Tracking" configuration set to an SNS topic, which
``config.urls`` forwards to ``django_ses.views.SESEventWebhookView`` at
``ses/events/``. ``notifications.ses_events`` turns each one into a row here:

- ``SentEmail`` is written the moment a message actually leaves via
  ``django_ses.SESBackend`` (the ``message_sent`` signal), keyed on SES's own
  ``MessageId``.
- ``EmailEvent`` is written for every SNS event that follows, matched back to
  the ``SentEmail`` it belongs to when the event's ``mail.messageId`` lines up
  with one we sent (a message SES was never asked to track, or one sent
  before this app existed, has no match — ``sent_email`` is then ``None``).

Together they are the source data for the staff deliverability dashboard —
the second half of #507, built on top of this schema.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class EmailKind(models.TextChoices):
    """What an outbound message *is*, independent of its SES event history.

    Set on the way out by ``notifications.emails.tag_kind`` (an
    ``X-SES-MESSAGE-TAGS: kind=<value>`` header, which SES echoes back on
    every event as ``mail.tags["kind"]``) and read back by
    ``kind_from_headers`` / ``kind_from_tags``. Values are restricted to
    ``[A-Za-z0-9_-]`` — SES's own message-tag alphabet.
    """

    BLOCK_DELIVERED = "block_delivered", _("Block delivered")
    COACH_INVITE = "coach_invite", _("Coach invite")
    INVITE_REMINDER = "invite_reminder", _("Invite reminder")
    COACH_REQUEST = "coach_request", _("Coach request")
    CONTACT_OWNER = "contact_owner", _("Contact form (owner copy)")
    CONTACT_ACK = "contact_ack", _("Contact form (sender ack)")
    MARGIN_ALERT = "margin_alert", _("Margin alert")
    ORDER_CONFIRMATION = "order_confirmation", _("Order confirmation")
    ACCOUNT_CONFIRMATION = "account_confirmation", _("Account confirmation")
    PASSWORD_RESET = "password_reset", _("Password reset")
    ACCOUNT_NOTICE = "account_notice", _("Account notice")
    HONEYPOT_ALERT = "honeypot_alert", _("Honeypot alert")
    OTHER = "other", _("Other")


# EmailKind values that are always plain text (no HTML alternative). SES only
# tracks opens via an invisible tracking pixel in an HTML part, so these can
# never register an open — the dashboard (presenters._by_kind) reports their
# open_rate as None (rendered "—", not a misleading 0%) rather than counting
# them against a metric they structurally cannot produce.
TEXT_ONLY_KINDS = frozenset(
    {
        EmailKind.ACCOUNT_CONFIRMATION,
        EmailKind.PASSWORD_RESET,
        EmailKind.ACCOUNT_NOTICE,
        EmailKind.HONEYPOT_ALERT,
    }
)


class SentEmail(models.Model):
    """One message actually handed to SES.

    Written by ``notifications.ses_events.record_sent_email`` off
    ``django_ses.signals.message_sent``, which fires from
    ``SESBackend.send_messages`` right after it stamps SES's own
    ``MessageId`` onto ``message.extra_headers["message_id"]`` — that value
    is ``ses_message_id`` below, the join key every later ``EmailEvent``
    looks itself up by.
    """

    ses_message_id = models.CharField(max_length=255, unique=True)
    kind = models.CharField(
        max_length=32,
        choices=EmailKind,
        default=EmailKind.OTHER,
        db_index=True,
    )
    recipient = models.EmailField(max_length=254, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_emails",
    )
    subject = models.CharField(max_length=998, blank=True)
    sent_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-sent_at"]
        verbose_name = "Sent email"
        verbose_name_plural = "Sent emails"

    def __str__(self):
        return f"{self.get_kind_display()} → {self.recipient} ({self.ses_message_id})"


class EmailEvent(models.Model):
    """One SES event (send, delivery, open, click, bounce, complaint).

    Idempotency is keyed on ``(sns_message_id, recipient)`` rather than on
    the SNS ``MessageId`` alone: SNS retries delivery on anything but a 200
    (so the same notification can arrive twice), and a single SES event can
    name more than one recipient (a bounce's ``bouncedRecipients``, a
    complaint's ``complainedRecipients``) — each becomes its own row sharing
    one ``sns_message_id``.
    """

    class EventType(models.TextChoices):
        SEND = "send", _("Send")
        DELIVERY = "delivery", _("Delivery")
        OPEN = "open", _("Open")
        CLICK = "click", _("Click")
        BOUNCE = "bounce", _("Bounce")
        COMPLAINT = "complaint", _("Complaint")

    sent_email = models.ForeignKey(
        SentEmail,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    event_type = models.CharField(
        max_length=16, choices=EventType.choices, db_index=True
    )
    # SES's MessageId for the underlying send (`mail.messageId`) — the same
    # value as SentEmail.ses_message_id when a match was found, but kept here
    # too (and indexed) so events survive/queryable even without a match.
    ses_message_id = models.CharField(max_length=255, db_index=True)
    # The SNS notification's own MessageId — half of the idempotency key.
    sns_message_id = models.CharField(max_length=255)
    recipient = models.EmailField(blank=True)
    kind = models.CharField(
        max_length=32,
        choices=EmailKind,
        default=EmailKind.OTHER,
        db_index=True,
    )
    occurred_at = models.DateTimeField(db_index=True)
    # Open/click only.
    user_agent = models.CharField(max_length=512, blank=True)
    # SES gives "Unlikely"/"Likely" (opens only) — stored verbatim.
    is_bot = models.CharField(max_length=16, blank=True)
    # Click only.
    link = models.URLField(max_length=2048, blank=True)
    # Bounce only.
    bounce_type = models.CharField(max_length=32, blank=True)
    bounce_subtype = models.CharField(max_length=32, blank=True)
    # The event's mail + event-specific payload, for anything not modeled
    # above (and as a debugging escape hatch).
    raw = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "Email event"
        verbose_name_plural = "Email events"
        constraints = [
            models.UniqueConstraint(
                fields=["sns_message_id", "recipient"],
                name="uniq_email_event_per_sns_message_recipient",
            ),
        ]
        indexes = [
            models.Index(fields=["event_type", "occurred_at"]),
            models.Index(fields=["kind", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.recipient}"
