from django.conf import settings
from django.core.mail import EmailMessage
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


def send_week_delivered_email(*, athlete, coach, plan, week, home_url) -> bool:
    """Email an athlete that their coach delivered a new training week.

    Meso athlete slice Phase 4a (decision S3): when a coach delivers a week, the
    athlete is notified through the channel that exists today — email via
    ``django-ses``. Web push waits on the PWA (Phase 4b).

    Args:
        athlete: the ``User`` who trains the plan (the recipient).
        coach: the ``User`` who delivered the week.
        plan: the delivered week's ``Plan`` (for its title).
        week: the delivered ``Week`` (for its index label).
        home_url: absolute URL of the athlete's training surface (``/meso/me/``).

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
    }
    subject = render_to_string(
        "notifications/week_delivered_subject.txt", context
    ).strip()
    msg_plain = render_to_string("notifications/week_delivered.md", context)
    msg_html = render_to_string("notifications/week_delivered.html", context)
    send_mail(
        subject=subject,
        message=msg_plain,
        html_message=msg_html,
        from_email=None,  # defaults to settings.DEFAULT_FROM_EMAIL
        recipient_list=[athlete.email],
        fail_silently=False,
    )
    return True
