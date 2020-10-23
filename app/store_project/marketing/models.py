import logging
import uuid

from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.utils.translation import ugettext_lazy as _

import markdown

from markdownx.models import MarkdownxField


logger = logging.getLogger(__name__)


class BaseModel(models.Model):
    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Email(BaseModel):
    sender = models.CharField(
        _("From"),
        max_length=150,
        default=settings.DEFAULT_FROM_EMAIL,
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("To"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="emails_received",
    )
    subject = models.CharField(
        _("Subject"),
        max_length=78,
    )
    sent = models.BooleanField(
        default=False,
    )
    html_body = MarkdownxField(
        _("HTML content, in markdown"),
        default="",
    )
    text_body = models.TextField(
        _("Text content"),
        default="",
    )

    def __str__(self):
        return f"{self.subject} | {self.recipient.email}"

    def send(self):
        """Send this composed email."""
        try:
            message = send_mail(
                self.subject,
                self.text_body,
                self.sender,
                [self.recipient.email],
                html_message=markdown.markdown(self.html_body),
            )
        except Exception as e:
            logger.error(f"Failed sending email: {e}")

        self.sent = True
        self.save(update_fields=["sent"])
        logger.info(f"EMAIL SENT: {message}")
