import logging
from email.utils import formataddr
from email.utils import parseaddr

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from store_project.meso.names import athlete_name
from store_project.meso.names import clean_name
from store_project.meso.names import coach_name

from .models import EmailKind

logger = logging.getLogger(__name__)


class ContactOwnerCopyNotSent(RuntimeError):
    """The owner's copy of a contact-form submission could not be delivered.

    Raised by ``send_contact_emails`` when ``EmailMessage.send()`` reports
    ``0`` for the owner's copy -- which happens without raising whenever
    ``AWS_SES_USE_BLACKLIST`` causes django-ses's ``SESBackend`` to filter out
    every recipient (e.g. ``settings.DEFAULT_FROM_EMAIL`` itself is in
    ``BlacklistedEmail``). A filtered owner copy must behave like a failed
    owner copy, not a silent success.
    """


# SES's own custom message-tag header. Set on the way out by tag_kind(); SES
# copies it onto every event it later reports for the message, as
# mail.tags["kind"] (read back by kind_from_tags()) — see
# notifications.ses_events for the receiver side.
SES_MESSAGE_TAGS_HEADER = "X-SES-MESSAGE-TAGS"


def tag_kind(message, kind: EmailKind) -> None:
    """Tag an outgoing message with its ``EmailKind`` for SES and the dashboard.

    Sets SES's ``X-SES-MESSAGE-TAGS`` header to ``kind=<value>``. SES echoes
    custom message tags back on every event (open, click, bounce, ...) it
    reports for the message as ``mail.tags["kind"]``, which
    ``notifications.ses_events`` reads via ``kind_from_tags`` to denormalise
    ``EmailEvent.kind`` even when the ``SentEmail`` row can't be matched.

    SES restricts message-tag values to ``[A-Za-z0-9_-]`` — exactly the
    alphabet ``EmailKind``'s values use, so no escaping is needed here.

    Args:
        message: any ``django.core.mail`` message instance (mutated in place).
        kind: the ``EmailKind`` (or its string value) this message is.
    """
    message.extra_headers[SES_MESSAGE_TAGS_HEADER] = f"kind={kind}"


def kind_from_headers(extra_headers: dict) -> EmailKind:
    """Recover the ``EmailKind`` ``tag_kind()`` set, from ``message.extra_headers``.

    Used by ``notifications.ses_events.record_sent_email``, which still has
    the outgoing ``EmailMessage`` in hand (via ``message_sent``) rather than
    an SES event's ``mail.tags`` — see ``kind_from_tags`` for that side.

    Returns ``EmailKind.OTHER`` when the header is missing or unrecognised.
    """
    raw = (extra_headers or {}).get(SES_MESSAGE_TAGS_HEADER, "")
    for pair in raw.split(","):
        key, _, value = pair.partition("=")
        if key.strip() == "kind":
            try:
                return EmailKind(value.strip())
            except ValueError:
                return EmailKind.OTHER
    return EmailKind.OTHER


def kind_from_tags(tags: dict) -> EmailKind:
    """Recover the ``EmailKind`` from an SES event's ``mail.tags`` dict.

    SES echoes the ``X-SES-MESSAGE-TAGS`` header ``tag_kind()`` set back on
    every event for a message sent through the "Tracking" configuration set,
    as ``tags["kind"]`` (a list — SES's tag values are always lists).

    Returns ``EmailKind.OTHER`` when the tag is missing or unrecognised (for
    instance, a message sent before this app existed, or via some other
    path).
    """
    values = (tags or {}).get("kind") or []
    if not values:
        return EmailKind.OTHER
    try:
        return EmailKind(values[0])
    except ValueError:
        return EmailKind.OTHER


def send_contact_emails(message_subject: str, message: str, user_email: str) -> bool:
    """Take the fields from a contact form submission and send two emails.

    The two emails are:
        1. A notification to the site owner, carrying the whole message.
        2. An acknowledgement to the person who filled in the form, so they know
           it arrived rather than vanishing into nothing.

    The acknowledgement deliberately repeats **nothing** from the form -- not
    the subject, not the body. Both are attacker-controlled, and the address it
    goes to is attacker-chosen, so echoing them turns this form into a way to
    send arbitrary text to arbitrary strangers over our own domain. That costs
    us sender reputation on SES and is exactly what the spam runs were using it
    for. The owner's copy still contains everything.

    Args:
        message_subject: the subject the sender typed (owner's copy only).
        message: the message body (owner's copy only).
        user_email: the sender's address, used as the owner's reply-to and as
            the acknowledgement's recipient.

    Returns:
        ``True`` if the acknowledgement reached the sender's address, ``False``
        if it could not be sent.

    Raises:
        ContactOwnerCopyNotSent: the owner's copy is sent first and is not
            best-effort. If ``send()`` raises, that exception propagates
            as-is. If ``send()`` instead reports ``0`` -- which happens
            without raising whenever ``AWS_SES_USE_BLACKLIST`` filters out
            every recipient, e.g. the owner's own address is blacklisted --
            this is raised instead and the acknowledgement is not attempted,
            because a message we cannot deliver to the owner is a message
            that was lost.
    """
    subject = render_to_string(
        "notifications/contact_email_subject.txt", {"subject": message_subject}
    ).strip()

    # Email the admin
    admin_text_msg = render_to_string(
        "notifications/contact_admin.md", {"msg": message}
    )
    email_for_admin = EmailMessage(
        subject,
        admin_text_msg,
        settings.SERVER_EMAIL,
        [
            settings.DEFAULT_FROM_EMAIL,
        ],
        reply_to=[user_email],
    )
    tag_kind(email_for_admin, EmailKind.CONTACT_OWNER)
    sent_to_owner = email_for_admin.send()
    if not sent_to_owner:
        logger.error(
            "Contact form owner copy not sent: %s appears to be filtered "
            "(e.g. blacklisted). Skipping the sender acknowledgement.",
            settings.DEFAULT_FROM_EMAIL,
        )
        raise ContactOwnerCopyNotSent(
            f"The owner address ({settings.DEFAULT_FROM_EMAIL}) appears to be "
            "blacklisted or otherwise filtered -- the contact form submission "
            "was not delivered."
        )

    # Acknowledge to the sender. Best-effort: a bounced or rejected
    # acknowledgement must not lose a message the owner has already received,
    # so a failure here is reported back, not raised.
    ack_subject = render_to_string("notifications/contact_user_subject.txt").strip()
    ack_text_msg = render_to_string("notifications/contact_user.md")
    email_for_user = EmailMessage(
        ack_subject,
        ack_text_msg,
        settings.SERVER_EMAIL,
        [
            user_email,
        ],
        reply_to=[
            settings.DEFAULT_FROM_EMAIL,
        ],
    )
    tag_kind(email_for_user, EmailKind.CONTACT_ACK)
    try:
        sent = email_for_user.send()
    except Exception:
        logger.warning(
            "Could not send the contact acknowledgement to the sender.",
            exc_info=True,
        )
        return False
    return sent > 0


def first_name(full) -> str:
    """First whitespace-separated token of a name; ``""`` when there is none."""
    cleaned = clean_name(full)
    return cleaned.split(" ", 1)[0] if cleaned else ""


DISPLAY_NAME_MAX = 64


def _safe_display_name(raw) -> str:
    """A coach's free-text name made safe for a From display (#671).

    Drops ``<>@"`` (so a name like ``support@paypal.com`` cannot pose as the
    sender's address) and every control character, folds whitespace, and caps the
    length. May return ``""``; the caller falls back.
    """
    kept = "".join(
        ch
        for ch in str(raw or "")
        if ch not in '<>@"' and (ch.isspace() or ch.isprintable())
    )
    return clean_name(kept)[:DISPLAY_NAME_MAX].strip()


def client_email_identity(coach) -> dict:
    """From + Reply-To kwargs for an email a coach's action sends to a client.

    The envelope address stays ``settings.DEFAULT_FROM_EMAIL``'s (SES verifies
    it); only the display name changes, to ``"<Coach> via Mastering Fitness"``,
    and replies go to the coach. ``formataddr`` quotes/encodes the name, and
    ``clean_name`` collapses newlines and other whitespace, so a hostile display
    name cannot inject headers. A coach with no email gets no Reply-To.
    """
    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    name = (
        _safe_display_name(coach_name(coach))
        or _safe_display_name(getattr(coach, "name", ""))
        or "Your coach"
    )
    identity = {"from_email": formataddr((f"{name} via Mastering Fitness", address))}
    identity["reply_to"] = [coach.email] if coach.email else []
    return identity


def _greeting_name(user, label="") -> str:
    """First name for a "Hi Jordan," greeting; ``""`` unless a real name exists.

    Only the athlete's own name or the coach's label count -- never the email
    stem ``athlete_name`` falls back to.
    """
    return first_name(getattr(user, "name", "") or label)


def send_coach_invite_email(*, coach, email, accept_url, recipient_name="") -> bool:
    """Email an athlete a tokened link to claim a coach's training invite.

    Meso N4 (athlete onboarding): a coach invites a person by email; this sends
    them the claim link. Whoever follows it while authenticated materializes the
    coach↔athlete relationship (``CoachInvite.accept``). Email is the channel that
    exists today — ``django-ses`` in production.

    Args:
        coach: the inviting ``User`` (for the message's "from" name).
        email: the invited address (the recipient).
        accept_url: absolute URL of the claim page (``/meso/claim/<token>/``).
        recipient_name: the name the coach typed for the invitee (``CoachInvite.label``);
            only its first name is used, in the greeting. Empty greets "Hi,".

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because there is no
        address to send to, or because the backend accepted no recipients (e.g.
        every recipient is blacklisted — ``AWS_SES_USE_BLACKLIST``).

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the request on a bounced email should treat this as best-effort.
    """
    if not email:
        return False
    context = {
        "coach_name": coach_name(coach),
        "greeting_name": first_name(recipient_name),
        "accept_url": accept_url,
    }
    subject = render_to_string(
        "notifications/coach_invite_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_invite.md", context)
    msg_html = render_to_string("notifications/coach_invite.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        to=[email],
        **client_email_identity(coach),
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.COACH_INVITE)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_coach_invite_reminder_email(
    *, coach, email, accept_url, recipient_name=""
) -> bool:
    """Remind an athlete that a coach's claim link is about to expire.

    Meso N4 Phase 4 (invite lifecycle): a pending ``CoachInvite`` nears its TTL
    without being claimed. The ``meso_remind_expiring_invites`` sweep sends this
    nudge so the link doesn't quietly lapse. The reminder peer of
    ``send_coach_invite_email`` — same claim link, "expiring soon" framing.

    Args:
        coach: the inviting ``User`` (for the message's "from" name).
        email: the invited address (the recipient).
        accept_url: absolute URL of the claim page (``/meso/claim/<token>/``).
        recipient_name: the name the coach typed for the invitee (``CoachInvite.label``);
            only its first name is used, in the greeting. Empty greets "Hi,".

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because there is no
        address to send to, or because the backend accepted no recipients (e.g.
        every recipient is blacklisted — ``AWS_SES_USE_BLACKLIST``).

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the sweep on a bounced email should treat this as best-effort.
    """
    if not email:
        return False
    context = {
        "coach_name": coach_name(coach),
        "greeting_name": first_name(recipient_name),
        "accept_url": accept_url,
    }
    subject = render_to_string(
        "notifications/coach_invite_reminder_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_invite_reminder.md", context)
    msg_html = render_to_string("notifications/coach_invite_reminder.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        to=[email],
        **client_email_identity(coach),
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.INVITE_REMINDER)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_coach_request_email(*, athlete, coach, roster_url) -> bool:
    """Email a coach that an athlete has asked to train under them.

    Meso N4 Phase 2 (athlete onboarding, the reverse direction): an athlete who
    already has an account asks to be coached. This notifies the coach so they
    can accept or decline on their roster — the symmetric counterpart to
    ``send_coach_invite_email``.

    Args:
        athlete: the requesting ``User`` (named in the message).
        coach: the ``User`` being asked to coach (the recipient).
        roster_url: absolute URL of the coach's roster (``/meso/``), where the
            pending request is accepted or declined.

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because the coach
        has no email address on file, or because the backend accepted no
        recipients (e.g. every recipient is blacklisted —
        ``AWS_SES_USE_BLACKLIST``).

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the request on a bounced email should treat this as best-effort.
    """
    if not coach.email:
        return False
    context = {
        "athlete_name": athlete_name(athlete),
        "roster_url": roster_url,
    }
    subject = render_to_string(
        "notifications/coach_request_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_request.md", context)
    msg_html = render_to_string("notifications/coach_request.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        to=[coach.email],
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.COACH_REQUEST)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_athlete_waiting_email(*, athlete, coach, roster_url, athlete_label="") -> bool:
    """Email a coach that an athlete accepted but there is no seat for them yet.

    #649: a free coach at the athlete cap can't activate another athlete, so the
    acceptance is parked (``accepted_waiting``). This tells the coach, with one
    link to the roster where Start free trial / Subscribe live. Nothing is ever
    sent to the athlete about it.

    Returns ``True`` if sent, ``False`` if skipped (no email on file or the
    backend accepted no recipients). Raises a mail backend exception; callers
    treat this as best-effort.
    """
    if not coach.email:
        return False
    context = {
        "athlete_name": athlete_name(athlete, athlete_label),
        "roster_url": roster_url,
    }
    subject = render_to_string(
        "notifications/athlete_waiting_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/athlete_waiting.md", context)
    msg_html = render_to_string("notifications/athlete_waiting.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        to=[coach.email],
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.ATHLETE_WAITING)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_invite_accepted_email(
    *, athlete, coach, roster_url, athlete_label="", template_title=""
) -> bool:
    """Email a coach that the athlete they invited accepted (#643).

    ``template_title`` is the program the coach wrote ahead of the accept (see
    ``Plan.for_invite``); when given, the email says it is ready to start. Goes
    TO the coach, so it uses the plain default From with no Reply-To.

    Returns ``True`` if sent, ``False`` if skipped (no email on file or the
    backend accepted no recipients). Raises a mail backend exception; callers
    treat this as best-effort.
    """
    if not coach.email:
        return False
    context = {
        "athlete_name": athlete_name(athlete, athlete_label),
        "template_title": " ".join((template_title or "").split()),
        "roster_url": roster_url,
    }
    subject = render_to_string(
        "notifications/invite_accepted_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/invite_accepted.md", context)
    msg_html = render_to_string("notifications/invite_accepted.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        to=[coach.email],
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.INVITE_ACCEPTED)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_margin_alert_email(*, alerts, month_label, threshold) -> bool:
    """Email the owner that paying coaches' agent cost is eating their margin.

    Meso agent-usage tracking Phase 3: the monthly ``meso-agent-margin-alert``
    sweep finds paying coaches whose estimated agent cost has crossed a fraction of
    their plan revenue (``meso/billing/agent_usage_report.margin_alerts``) and
    sends this internal, owner-facing summary so the $1/seat tail risk surfaces
    before the month closes. The recipients are ``settings.ADMINS`` (the owner),
    not a coach — this is operational, not customer-facing.

    Args:
        alerts: the at-risk ``CoachUsage`` rows (worst cost-to-revenue ratio
            first), each carrying its label, revenue, totals, and margin.
        month_label: the report month, e.g. ``"2026-06"`` (subject + body).
        threshold: the alert fraction as a ``Decimal`` (``0.5`` renders "50%").

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because there were no
        alerts or no admin address to send to, or because the backend accepted no
        recipients (e.g. every recipient is blacklisted — ``AWS_SES_USE_BLACKLIST``).

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail a scheduled sweep on a bounced email should treat this as best-effort.
    """
    recipients = [email for _name, email in settings.ADMINS if email]
    if not alerts or not recipients:
        return False
    rows = [
        {
            "label": coach.label,
            "billing_status": coach.billing_status,
            "seats": coach.billable_seats,
            "runs": coach.totals.runs,
            "cost": f"{coach.totals.cost:.2f}",
            "revenue": f"{coach.revenue:.2f}",
            "margin": f"{coach.margin:.2f}",
            "ratio_pct": f"{coach.cost_to_revenue_ratio * 100:.0f}",
        }
        for coach in alerts
    ]
    context = {
        "rows": rows,
        "count": len(rows),
        "month_label": month_label,
        "threshold_pct": f"{threshold * 100:.0f}",
    }
    subject = render_to_string(
        "notifications/margin_alert_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/margin_alert.md", context)
    msg_html = render_to_string("notifications/margin_alert.html", context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        from_email=settings.SERVER_EMAIL,  # the robot, not the owner's own address
        to=recipients,
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.MARGIN_ALERT)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_block_delivered_email(
    *,
    athlete,
    coach,
    plan,
    week_count,
    home_url,
    unsubscribe_url=None,
    athlete_label="",
) -> bool:
    """Email an athlete that their coach delivered a whole new training block.

    The deliver nudge (Meso P3; the per-week variant was retired with the 2d
    live+notify model): the deliver path nudges about a whole mesocycle at
    once, so the athlete gets a single email naming the block's week count, not
    one per week. When ``unsubscribe_url`` is given, the message carries the
    ``List-Unsubscribe`` + ``List-Unsubscribe-Post`` headers (RFC 8058
    one-click) and a visible footer link, so Gmail/Apple Mail render a working
    unsubscribe control — the caller is responsible for *honoring* an opt-out
    (it gates this call); this function only advertises the link.

    Args:
        athlete: the ``User`` who trains the plan (the recipient).
        coach: the ``User`` who delivered the block.
        plan: the delivered ``Plan`` (for its title).
        week_count: how many live weeks were delivered (drives the "N weeks" copy).
        home_url: absolute URL of the athlete's training surface (``/meso/me/``).
        unsubscribe_url: absolute URL of the tokened, login-free unsubscribe
            page; ``None`` omits the headers and footer.
        athlete_label: this coach's fallback name for an unnamed athlete.

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because the athlete
        has no email address on file, or because the backend accepted no
        recipients (e.g. every recipient is blacklisted —
        ``AWS_SES_USE_BLACKLIST``).

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not let a delivery fail on a bounced email should treat this as best-effort.
    """
    if not athlete.email:
        return False
    context = {
        "athlete_name": athlete_name(athlete, athlete_label),
        "greeting_name": _greeting_name(athlete, athlete_label),
        "coach_name": coach_name(coach),
        "plan_title": plan.title,
        "week_count": week_count,
        "home_url": home_url,
        "unsubscribe_url": unsubscribe_url,
    }
    subject = render_to_string(
        "notifications/block_delivered_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/block_delivered.md", context)
    msg_html = render_to_string("notifications/block_delivered.html", context)
    headers = {}
    if unsubscribe_url:
        # RFC 2369 + RFC 8058: a header List-Unsubscribe (https for one-click)
        # plus List-Unsubscribe-Post turns it into a one-click mail-client button.
        headers["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        to=[athlete.email],
        headers=headers,
        **client_email_identity(coach),
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.BLOCK_DELIVERED)
    sent = message.send(fail_silently=False)
    return sent > 0


def send_relationship_ended_email(
    *,
    athlete,
    coach,
    home_url,
    unsubscribe_url=None,
    athlete_label="",
) -> bool:
    """Tell an athlete their coach ended the coaching relationship (#651).

    Only sent for a coach-initiated end. The caller gates the athlete's
    delivery-email opt-out (it honours ``athlete_opted_out``); this function
    only advertises the unsubscribe link, like ``send_block_delivered_email``.

    Returns ``True`` if a message was sent, ``False`` if the athlete has no
    email or the backend accepted no recipients. Raises on a mail backend error,
    so callers must treat it as best-effort.
    """
    if not athlete.email:
        return False
    context = {
        "athlete_name": athlete_name(athlete, athlete_label),
        "greeting_name": _greeting_name(athlete, athlete_label),
        "coach_name": coach_name(coach),
        "home_url": home_url,
        "unsubscribe_url": unsubscribe_url,
    }
    subject = render_to_string(
        "notifications/relationship_ended_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/relationship_ended.md", context)
    msg_html = render_to_string("notifications/relationship_ended.html", context)
    headers = {}
    if unsubscribe_url:
        headers["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        to=[athlete.email],
        headers=headers,
        **client_email_identity(coach),
    )
    message.attach_alternative(msg_html, "text/html")
    tag_kind(message, EmailKind.RELATIONSHIP_ENDED)
    sent = message.send(fail_silently=False)
    return sent > 0
