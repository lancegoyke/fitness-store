"""Issue #514: the /admin/ honeypot alert carries an EmailKind.

``admin_honeypot.listeners.notify_admins`` used to call Django's own
``mail_admins()``, which can't carry the ``X-SES-MESSAGE-TAGS`` header --
it now builds the equivalent message by hand and tags it
``EmailKind.HONEYPOT_ALERT`` via ``notifications.emails.tag_kind``.

The listener is only ever wired up from ``AdminHoneypotConfig.ready()``
(mirroring ``store_project.users.apps.UsersConfig.ready()``'s own
``import store_project.users.signals``) -- without that import the
module-level ``honeypot.connect(notify_admins)`` in ``listeners.py`` never
runs and no alert is ever sent. These tests exercise the whole path end to
end through the real honeypot login view, which is also how that wiring
gets proven.
"""

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from store_project.notifications.models import EmailKind

pytestmark = pytest.mark.django_db

TAG_HEADER = "X-SES-MESSAGE-TAGS"


class TestHoneypotAlert:
    def test_login_attempt_sends_a_tagged_alert_to_admins(self, client):
        client.post(
            reverse("admin_honeypot:login"),
            {"username": "admin", "password": "letmein"},
        )

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert (
            message.extra_headers[TAG_HEADER]
            == f"kind={EmailKind.HONEYPOT_ALERT.value}"
        )
        assert message.to == [addr for _name, addr in settings.ADMINS]
        assert message.from_email == settings.SERVER_EMAIL
        assert message.subject.startswith(settings.EMAIL_SUBJECT_PREFIX)

    def test_skips_sending_when_admins_is_empty(self, client, settings):
        settings.ADMINS = []

        client.post(
            reverse("admin_honeypot:login"),
            {"username": "admin", "password": "letmein"},
        )

        assert len(mail.outbox) == 0
