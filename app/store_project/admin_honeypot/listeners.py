from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import reverse

from store_project.admin_honeypot.signals import honeypot
from store_project.notifications.emails import tag_kind
from store_project.notifications.models import EmailKind


def notify_admins(instance, request, **kwargs):
    """Alert settings.ADMINS of an attempted /admin/ login (issue #514).

    Used to be a single ``mail_admins(subject=subject, message=message)``
    call, which can't carry the ``X-SES-MESSAGE-TAGS`` header ``tag_kind()``
    needs -- this builds the same message by hand (mirroring
    ``django.core.mail.mail_admins``: prefixed subject, ``SERVER_EMAIL`` as
    the sender, every ``settings.ADMINS`` address as a recipient, skipped
    entirely when ``ADMINS`` is empty) and tags it
    ``EmailKind.HONEYPOT_ALERT`` before sending.
    """
    recipients = [address for _name, address in settings.ADMINS]
    if not recipients:
        return
    path = reverse("admin:admin_honeypot_loginattempt_change", args=(instance.pk,))
    admin_detail_url = "http://{0}{1}".format(request.get_host(), path)
    context = {
        "request": request,
        "instance": instance,
        "admin_detail_url": admin_detail_url,
    }
    subject = render_to_string("admin_honeypot/email_subject.txt", context).strip()
    message_body = render_to_string("admin_honeypot/email_message.txt", context).strip()
    message = EmailMessage(
        subject=f"{settings.EMAIL_SUBJECT_PREFIX}{subject}",
        body=message_body,
        from_email=settings.SERVER_EMAIL,
        to=recipients,
    )
    tag_kind(message, EmailKind.HONEYPOT_ALERT)
    message.send(fail_silently=False)


if getattr(settings, "ADMIN_HONEYPOT_EMAIL_ADMINS", True):
    honeypot.connect(notify_admins)
