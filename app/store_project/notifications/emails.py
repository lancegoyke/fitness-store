from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string


def send_contact_emails(message_subject: str, message: str, user_email: str) -> None:
    """Takes the fields from a user-submitted form and sends two emails.

    The two emails are:
        1. A confirmation email to the user submitting the form.
        2. A notification email to the DEFAULT_FROM_EMAIL located in settings.
    """
    subject = render_to_string(
        "notifications/contact_email_subject.txt", {"subject": message_subject}
    )
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
