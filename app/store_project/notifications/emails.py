from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail import EmailMultiAlternatives
from django.core.mail import send_mail
from django.template.loader import render_to_string


def send_contact_emails(message_subject: str, message: str, user_email: str) -> None:
    """Takes the fields from a user-submitted form and sends two emails.

    The two emails are:
        1. A confirmation email to the user submitting the form.
        2. A notification email to the DEFAULT_FROM_EMAIL located in settings.
    """
    subject = render_to_string(
        "notifications/contact_email_subject.txt", {"subject": message_subject}
    ).strip()
    msg = message

    # Email the admin
    admin_text_msg = render_to_string("notifications/contact_admin.md", {"msg": msg})
    email_for_admin = EmailMessage(
        subject,
        admin_text_msg,
        settings.SERVER_EMAIL,
        [
            settings.DEFAULT_FROM_EMAIL,
        ],
        reply_to=[user_email],
    )
    email_for_admin.send()

    # TODO: Email the user
    user_text_msg = render_to_string("notifications/contact_user.md", {"msg": msg})
    email_for_user = EmailMessage(
        subject,
        user_text_msg,
        settings.SERVER_EMAIL,
        [
            user_email,
        ],
        reply_to=[
            settings.DEFAULT_FROM_EMAIL,
        ],
    )
    email_for_user.send()


def send_coach_invite_email(*, coach, email, accept_url) -> bool:
    """Email an athlete a tokened link to claim a coach's training invite.

    Meso N4 (athlete onboarding): a coach invites a person by email; this sends
    them the claim link. Whoever follows it while authenticated materializes the
    coach↔athlete relationship (``CoachInvite.accept``). Email is the channel that
    exists today — ``django-ses`` in production.

    Args:
        coach: the inviting ``User`` (for the message's "from" name).
        email: the invited address (the recipient).
        accept_url: absolute URL of the claim page (``/meso/claim/<token>/``).

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because there is no
        address to send to.

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the request on a bounced email should treat this as best-effort.
    """
    if not email:
        return False
    context = {
        "coach_name": coach.display_name(),
        "accept_url": accept_url,
    }
    subject = render_to_string(
        "notifications/coach_invite_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_invite.md", context)
    msg_html = render_to_string("notifications/coach_invite.html", context)
    send_mail(
        subject=subject,
        message=msg_plain,
        html_message=msg_html,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        recipient_list=[email],
        fail_silently=False,
    )
    return True


def send_coach_invite_reminder_email(*, coach, email, accept_url) -> bool:
    """Remind an athlete that a coach's claim link is about to expire.

    Meso N4 Phase 4 (invite lifecycle): a pending ``CoachInvite`` nears its TTL
    without being claimed. The ``meso_remind_expiring_invites`` sweep sends this
    nudge so the link doesn't quietly lapse. The reminder peer of
    ``send_coach_invite_email`` — same claim link, "expiring soon" framing.

    Args:
        coach: the inviting ``User`` (for the message's "from" name).
        email: the invited address (the recipient).
        accept_url: absolute URL of the claim page (``/meso/claim/<token>/``).

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because there is no
        address to send to.

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the sweep on a bounced email should treat this as best-effort.
    """
    if not email:
        return False
    context = {
        "coach_name": coach.display_name(),
        "accept_url": accept_url,
    }
    subject = render_to_string(
        "notifications/coach_invite_reminder_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_invite_reminder.md", context)
    msg_html = render_to_string("notifications/coach_invite_reminder.html", context)
    send_mail(
        subject=subject,
        message=msg_plain,
        html_message=msg_html,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        recipient_list=[email],
        fail_silently=False,
    )
    return True


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
        has no email address on file.

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not fail the request on a bounced email should treat this as best-effort.
    """
    if not coach.email:
        return False
    context = {
        "athlete_name": athlete.display_name(),
        "roster_url": roster_url,
    }
    subject = render_to_string(
        "notifications/coach_request_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/coach_request.md", context)
    msg_html = render_to_string("notifications/coach_request.html", context)
    send_mail(
        subject=subject,
        message=msg_plain,
        html_message=msg_html,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        recipient_list=[coach.email],
        fail_silently=False,
    )
    return True


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
        alerts or no admin address to send to.

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
    send_mail(
        subject=subject,
        message=msg_plain,
        html_message=msg_html,
        from_email=settings.SERVER_EMAIL,  # the robot, not the owner's own address
        recipient_list=recipients,
        fail_silently=False,
    )
    return True


def send_week_delivered_email(
    *, athlete, coach, plan, week, home_url, unsubscribe_url=None
) -> bool:
    """Email an athlete that their coach delivered a new training week.

    Meso athlete slice Phase 4a (decision S3): when a coach delivers a week, the
    athlete is notified through the channel that exists today — email via
    ``django-ses``. Web push waits on the PWA (Phase 4b).

    When ``unsubscribe_url`` is given, the message carries the email
    best-practice ``List-Unsubscribe`` + ``List-Unsubscribe-Post`` headers
    (RFC 8058 one-click) and a visible footer link, so Gmail/Apple Mail render a
    working unsubscribe control. The caller is responsible for *honoring* an
    opt-out (it gates this call); this function only advertises the link.

    Args:
        athlete: the ``User`` who trains the plan (the recipient).
        coach: the ``User`` who delivered the week.
        plan: the delivered week's ``Plan`` (for its title).
        week: the delivered ``Week`` (for its index label).
        home_url: absolute URL of the athlete's training surface (``/meso/me/``).
        unsubscribe_url: absolute URL of the tokened, login-free unsubscribe
            page; ``None`` omits the headers and footer.

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because the athlete
        has no email address on file.

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not let a delivery fail on a bounced email should treat this as best-effort.
    """
    if not athlete.email:
        return False
    context = {
        "athlete_name": athlete.display_name(),
        "coach_name": coach.display_name(),
        "plan_title": plan.title,
        "week_label": f"Week {week.index}",
        "home_url": home_url,
        "unsubscribe_url": unsubscribe_url,
    }
    subject = render_to_string(
        "notifications/week_delivered_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/week_delivered.md", context)
    msg_html = render_to_string("notifications/week_delivered.html", context)
    headers = {}
    if unsubscribe_url:
        # RFC 2369 + RFC 8058: a header List-Unsubscribe (https for one-click)
        # plus List-Unsubscribe-Post turns it into a one-click mail-client button.
        headers["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message = EmailMultiAlternatives(
        subject=subject,
        body=msg_plain,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        to=[athlete.email],
        headers=headers,
    )
    message.attach_alternative(msg_html, "text/html")
    message.send(fail_silently=False)
    return True


def send_block_delivered_email(
    *, athlete, coach, plan, week_count, home_url, unsubscribe_url=None
) -> bool:
    """Email an athlete that their coach delivered a whole new training block.

    The block-level peer of ``send_week_delivered_email`` (Meso P3): the
    individual deliver path releases a whole mesocycle at once, so the athlete
    gets a single email naming the block's week count, not one per week. Same
    ``List-Unsubscribe`` (RFC 8058 one-click) wiring and best-effort contract as
    the per-week email — the caller gates the opt-out; this only advertises it.

    Args:
        athlete: the ``User`` who trains the plan (the recipient).
        coach: the ``User`` who delivered the block.
        plan: the delivered ``Plan`` (for its title).
        week_count: how many live weeks were delivered (drives the "N weeks" copy).
        home_url: absolute URL of the athlete's training surface (``/meso/me/``).
        unsubscribe_url: absolute URL of the tokened, login-free unsubscribe
            page; ``None`` omits the headers and footer.

    Returns:
        ``True`` if a message was sent, ``False`` if skipped because the athlete
        has no email address on file.

    Raises a mail backend exception (``fail_silently=False``); callers that must
    not let a delivery fail on a bounced email should treat this as best-effort.
    """
    if not athlete.email:
        return False
    context = {
        "athlete_name": athlete.display_name(),
        "coach_name": coach.display_name(),
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
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        to=[athlete.email],
        headers=headers,
    )
    message.attach_alternative(msg_html, "text/html")
    message.send(fail_silently=False)
    return True
