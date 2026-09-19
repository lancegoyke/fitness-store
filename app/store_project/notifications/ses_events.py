"""SES / SNS event receivers (issue #507, part 1).

Two independent signal sources feed the tracking tables in ``models.py``:

- ``django_ses.signals.message_sent`` fires from ``SESBackend.send_messages``
  right after SES accepts a message, with SES's own ``MessageId`` already
  stamped onto ``message.extra_headers["message_id"]``. ``record_sent_email``
  turns that into a ``SentEmail`` row — the join key every later event looks
  itself up by.
- ``django_ses.views.SESEventWebhookView`` (mounted at ``ses/events/``) parses
  each SNS notification and fires one of ``django_ses.signals.
  {send,delivery,open,click,bounce,complaint}_received``. One receiver per
  signal below turns that into an ``EmailEvent`` row.

Both signal sources are connected in ``NotificationsConfig.ready()``.

These receivers run inside the SNS webhook's request/response cycle (SNS
POSTs, the view returns 200), so most of what can go wrong in them must never
raise: a malformed payload, an unexpected shape, or any other non-database
exception is swallowed and logged, because retrying a fixed set of bad input
forever would just retry the same failure for hours (SNS interprets anything
but a 200 as failure and keeps redelivering). ``record_sent_email`` (the
``message_sent`` receiver — see its own docstring) always follows this rule,
because it runs inline in the request that is *sending* the email, where a
DB blip must never fail a send SES already accepted.

The ``*_received`` receivers below (via ``_record_event``) are the one
exception: a *transient* ``django.db.DatabaseError`` — Postgres restarting
mid-deploy, a dropped connection, anything that will clear up on its own —
is deliberately **not** swallowed. Letting it propagate turns the response
into a 500, so SNS retries, and the retry is safe: idempotency is handled by
keying ``EmailEvent`` rows on ``(sns_message_id, recipient)`` via
``get_or_create`` — the same pair the model's ``UniqueConstraint`` enforces.
A *permanent* ``django.db.DataError`` or ``django.db.IntegrityError`` — an
oversized value a ``varchar(n)`` column refuses, or a real constraint
violation retrying can never fix (``get_or_create`` already absorbs the
unique-race ``IntegrityError`` internally, so one that still escapes is a
genuine violation, e.g. a NOT NULL column handed ``None``) — is not: bounded
string fields are truncated to their column width and coerced away from
``None``/non-string before insert (``_fit``), and whatever that doesn't
catch is logged and swallowed rather than propagated, or SNS would
redeliver the same uninsertable event for hours. Every other exception in
``_record_event`` is still swallowed and logged, same as everywhere else.
"""

import json
import logging
from email.utils import parseaddr

from django.db import DatabaseError
from django.db import DataError
from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from store_project.users.models import User

from .emails import kind_from_headers
from .emails import kind_from_tags
from .models import EmailEvent
from .models import SentEmail

logger = logging.getLogger(__name__)


def record_sent_email(sender, message, **kwargs):
    """Write a ``SentEmail`` row for a message SES just accepted.

    Connected to ``django_ses.signals.message_sent``. Never fires under the
    locmem backend tests default to — ``django_ses.SESBackend.send_messages``
    is the only thing that sends it, always with SES's own ``MessageId``
    already on ``message.extra_headers["message_id"]``.

    A message with no ``message_id`` (anything that didn't go through
    ``SESBackend``, or a send that failed before SES assigned one) is skipped
    silently — there is nothing to key a row on. Never raises, deliberately
    including a ``DatabaseError``: unlike ``_record_event`` below, this runs
    inline in the request that is *sending* the email, so a transient DB
    blip here must not fail a send SES has already accepted.
    """
    try:
        ses_message_id = (message.extra_headers or {}).get("message_id")
        if not ses_message_id:
            logger.debug(
                "message_sent with no SES message_id; not recording a SentEmail row."
            )
            return

        recipient = ""
        if message.to:
            recipient = parseaddr(message.to[0])[1].lower()

        user = (
            User.objects.filter(email__iexact=recipient).first() if recipient else None
        )

        SentEmail.objects.get_or_create(
            ses_message_id=ses_message_id,
            defaults={
                "kind": kind_from_headers(message.extra_headers),
                "recipient": recipient,
                "user": user,
                "subject": message.subject or "",
            },
        )
    except Exception:
        logger.exception("Failed to record sent email for message_sent signal")


def _sns_message_id(raw_message) -> str:
    """The SNS envelope's own ``MessageId`` — half of the idempotency key."""
    try:
        return json.loads(raw_message).get("MessageId", "") or ""
    except (TypeError, ValueError):
        logger.warning("Could not parse SNS MessageId from the event's raw_message.")
        return ""


def _occurred_at(timestamp):
    """Parse an SES event timestamp, falling back to ``now`` if unparseable."""
    if timestamp:
        parsed = parse_datetime(timestamp)
        if parsed:
            return parsed
    return timezone.now()


def _fit(field_name: str, value) -> str:
    """Truncate ``value`` to ``EmailEvent``'s ``field_name`` column width.

    SES/SNS payloads are not bounded to our column widths — a redirect-heavy
    click ``link`` or a verbose ``userAgent`` can outgrow the model field
    that stores it. Postgres is strict about ``varchar(n)`` and raises
    ``django.db.DataError`` on an oversized insert (SQLite, which the test
    suite runs on, is not, so this is the thing that actually keeps
    production inserts from failing). Truncating first keeps a too-long
    payload insertable instead of relying solely on ``_record_event``'s
    ``DataError`` fallback below.

    ``None`` (an explicit JSON null, e.g. ``"bounceType": null``) or any
    other non-string value is coerced to ``""``: every field this is called
    on is a NOT NULL ``CharField``, and passing ``None`` straight through
    raises ``django.db.IntegrityError`` — a *permanent* failure (see
    ``_record_event``) that would otherwise make SNS retry forever.
    """
    if not isinstance(value, str):
        value = ""
    max_length = EmailEvent._meta.get_field(field_name).max_length
    if max_length is None:
        return value
    return value[:max_length]


def _record_event(
    *,
    event_type,
    mail_obj,
    event_obj,
    raw_message,
    recipients,
    timestamp,
    extra_fields=None,
):
    """Shared body for every ``*_received`` receiver below.

    Resolves the ``SentEmail`` match (if any) and the event's ``kind``, then
    writes one ``EmailEvent`` per recipient (skipping any recipient that
    isn't a non-empty string — SES/SNS payloads aren't guaranteed to be
    well-shaped), idempotent on ``(sns_message_id, recipient)`` via
    ``get_or_create`` (which already absorbs the unique-race
    ``IntegrityError`` itself, so no special case is needed for *that*).
    Every bounded string field is truncated to its column width and coerced
    away from ``None``/non-string first (``_fit``), since SES/SNS payloads
    aren't bounded to ours. A ``django.db.DatabaseError`` propagates — a
    transient failure should turn into a 500 so SNS retries the (idempotent)
    delivery rather than losing the event forever — *except*
    ``django.db.DataError`` or ``django.db.IntegrityError``, both permanent
    failures (an oversized value truncation didn't catch, or a real
    constraint violation that isn't the unique-race case ``get_or_create``
    already handles): those are logged and swallowed like any other bad
    payload, or SNS would redeliver an uninsertable event for hours.
    Everything else (malformed payloads, etc.) is likewise swallowed and
    logged, since a bad event must not take the webhook down with it.
    """
    try:
        mail_obj = mail_obj or {}
        ses_message_id = _fit("ses_message_id", mail_obj.get("messageId", "") or "")
        sns_message_id = _fit("sns_message_id", _sns_message_id(raw_message))
        if not sns_message_id:
            logger.warning(
                "%s event with no SNS MessageId; recording with an empty key.",
                event_type,
            )

        sent_email = (
            SentEmail.objects.filter(ses_message_id=ses_message_id).first()
            if ses_message_id
            else None
        )
        kind = (
            sent_email.kind if sent_email else kind_from_tags(mail_obj.get("tags", {}))
        )
        occurred_at = _occurred_at(timestamp)
        raw = {"mail": mail_obj, "event": event_obj}

        for recipient in recipients or []:
            if not isinstance(recipient, str) or not recipient:
                logger.warning(
                    "%s event with a non-string or empty recipient (%r); skipping it.",
                    event_type,
                    recipient,
                )
                continue
            recipient = _fit("recipient", recipient.lower())
            defaults = {
                "sent_email": sent_email,
                "event_type": event_type,
                "ses_message_id": ses_message_id,
                "kind": kind,
                "occurred_at": occurred_at,
                "raw": raw,
            }
            defaults.update(
                {key: _fit(key, value) for key, value in (extra_fields or {}).items()}
            )
            EmailEvent.objects.get_or_create(
                sns_message_id=sns_message_id,
                recipient=recipient,
                defaults=defaults,
            )
    except (DataError, IntegrityError):
        logger.exception(
            "Failed to record %s event: permanently uninsertable data", event_type
        )
    except DatabaseError:
        raise
    except Exception:
        logger.exception("Failed to record %s event", event_type)


def record_send(sender, mail_obj, send_obj, raw_message, **kwargs):
    """``send_received`` → one ``EmailEvent`` per ``mail.destination`` address."""
    mail_obj = mail_obj or {}
    _record_event(
        event_type=EmailEvent.EventType.SEND,
        mail_obj=mail_obj,
        event_obj=send_obj,
        raw_message=raw_message,
        recipients=mail_obj.get("destination", []),
        timestamp=mail_obj.get("timestamp"),
    )


def record_delivery(sender, mail_obj, delivery_obj, raw_message, **kwargs):
    """``delivery_received`` → one ``EmailEvent`` per ``delivery.recipients`` address."""
    delivery_obj = delivery_obj or {}
    _record_event(
        event_type=EmailEvent.EventType.DELIVERY,
        mail_obj=mail_obj,
        event_obj=delivery_obj,
        raw_message=raw_message,
        recipients=delivery_obj.get("recipients", []),
        timestamp=delivery_obj.get("timestamp"),
    )


def record_open(sender, mail_obj, open_obj, raw_message, **kwargs):
    """``open_received`` → one ``EmailEvent`` per ``mail.destination`` address."""
    mail_obj = mail_obj or {}
    open_obj = open_obj or {}
    _record_event(
        event_type=EmailEvent.EventType.OPEN,
        mail_obj=mail_obj,
        event_obj=open_obj,
        raw_message=raw_message,
        recipients=mail_obj.get("destination", []),
        timestamp=open_obj.get("timestamp"),
        extra_fields={
            "user_agent": open_obj.get("userAgent", ""),
            "is_bot": open_obj.get("isBotEvent", ""),
        },
    )


def record_click(sender, mail_obj, click_obj, raw_message, **kwargs):
    """``click_received`` → one ``EmailEvent`` per ``mail.destination`` address."""
    mail_obj = mail_obj or {}
    click_obj = click_obj or {}
    _record_event(
        event_type=EmailEvent.EventType.CLICK,
        mail_obj=mail_obj,
        event_obj=click_obj,
        raw_message=raw_message,
        recipients=mail_obj.get("destination", []),
        timestamp=click_obj.get("timestamp"),
        extra_fields={
            "user_agent": click_obj.get("userAgent", ""),
            "link": click_obj.get("link", ""),
        },
    )


def record_bounce(sender, mail_obj, bounce_obj, raw_message, **kwargs):
    """``bounce_received`` → one ``EmailEvent`` per ``bounce.bouncedRecipients`` entry."""
    bounce_obj = bounce_obj or {}
    recipients = [
        r.get("emailAddress")
        for r in bounce_obj.get("bouncedRecipients", [])
        if r.get("emailAddress")
    ]
    _record_event(
        event_type=EmailEvent.EventType.BOUNCE,
        mail_obj=mail_obj,
        event_obj=bounce_obj,
        raw_message=raw_message,
        recipients=recipients,
        timestamp=bounce_obj.get("timestamp"),
        extra_fields={
            "bounce_type": bounce_obj.get("bounceType", ""),
            "bounce_subtype": bounce_obj.get("bounceSubType", ""),
        },
    )


def record_complaint(sender, mail_obj, complaint_obj, raw_message, **kwargs):
    """``complaint_received`` → one ``EmailEvent`` per ``complaint.complainedRecipients`` entry."""
    complaint_obj = complaint_obj or {}
    recipients = [
        r.get("emailAddress")
        for r in complaint_obj.get("complainedRecipients", [])
        if r.get("emailAddress")
    ]
    _record_event(
        event_type=EmailEvent.EventType.COMPLAINT,
        mail_obj=mail_obj,
        event_obj=complaint_obj,
        raw_message=raw_message,
        recipients=recipients,
        timestamp=complaint_obj.get("timestamp"),
    )
