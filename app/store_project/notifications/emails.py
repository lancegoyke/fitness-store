from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string


def send_contact_emails(subject: str, message: str, user_email: str) -> None:
    """
    Takes the fields from a user-submitted form and sends two emails:
        1. A confirmation email to the user submitting the form.
        2. A notification email to the DEFAULT_FROM_EMAIL located in settings.
    """
    subject = f"Mastering Fitness Contact Form: {subject}"
    body = message
    from_email = settings.SERVER_EMAIL

    # Email the admin
    email_for_admin = EmailMessage(
        subject,
        body,
        from_email,
        [settings.DEFAULT_FROM_EMAIL, ],
        reply_to=[user_email],
    )
    email_for_admin.send()

    # TODO: Email the user
    email_for_user = EmailMessage(
        subject,
        body,
        from_email,
        [user_email, ],
        reply_to=[settings.DEFAULT_FROM_EMAIL, ],
    )
    email_for_user.send()
