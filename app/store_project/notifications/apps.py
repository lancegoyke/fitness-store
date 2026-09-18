from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class NotificationsConfig(AppConfig):
    name = "store_project.notifications"
    verbose_name = _("Notifications")

    def ready(self):
        """Connect the SES/SNS event receivers (issue #507, part 1).

        ``dispatch_uid`` per receiver guards against a double-connect if
        ``ready()`` ever runs twice (autoreload, some test-runner setups) —
        without it a redelivered signal would fire the receiver twice and
        defeat ``EmailEvent``'s own idempotency check.
        """
        from django_ses.signals import bounce_received
        from django_ses.signals import click_received
        from django_ses.signals import complaint_received
        from django_ses.signals import delivery_received
        from django_ses.signals import message_sent
        from django_ses.signals import open_received
        from django_ses.signals import send_received

        from .ses_events import record_bounce
        from .ses_events import record_click
        from .ses_events import record_complaint
        from .ses_events import record_delivery
        from .ses_events import record_open
        from .ses_events import record_send
        from .ses_events import record_sent_email

        message_sent.connect(
            record_sent_email, dispatch_uid="notifications.record_sent_email"
        )
        send_received.connect(record_send, dispatch_uid="notifications.record_send")
        delivery_received.connect(
            record_delivery, dispatch_uid="notifications.record_delivery"
        )
        open_received.connect(record_open, dispatch_uid="notifications.record_open")
        click_received.connect(record_click, dispatch_uid="notifications.record_click")
        bounce_received.connect(
            record_bounce, dispatch_uid="notifications.record_bounce"
        )
        complaint_received.connect(
            record_complaint, dispatch_uid="notifications.record_complaint"
        )
