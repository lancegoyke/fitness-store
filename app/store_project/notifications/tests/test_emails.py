"""Outbound-message tagging (issue #507, part 1).

Every ``notifications.emails`` sender now tags its message with an
``EmailKind`` (``tag_kind``) via SES's own custom message-tag header,
``X-SES-MESSAGE-TAGS: kind=<value>``. SES copies that header onto every event
it later reports for the message (as ``mail.tags["kind"]``), which is how
``notifications.ses_events`` denormalises ``EmailEvent.kind`` without needing
a ``SentEmail`` match. ``kind_from_headers``/``kind_from_tags`` are the two
readers: the first off a still-in-hand ``message.extra_headers`` (used by
``record_sent_email``), the second off an SES event's ``mail.tags`` dict
(used by the event receivers).
"""

import pytest
from django.core import mail

from store_project.notifications.emails import kind_from_headers
from store_project.notifications.emails import kind_from_tags
from store_project.notifications.emails import send_block_delivered_email
from store_project.notifications.emails import send_coach_invite_email
from store_project.notifications.emails import send_coach_invite_reminder_email
from store_project.notifications.emails import send_coach_request_email
from store_project.notifications.emails import send_contact_emails
from store_project.notifications.emails import send_margin_alert_email
from store_project.notifications.emails import tag_kind
from store_project.notifications.models import EmailKind
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

TAG_HEADER = "X-SES-MESSAGE-TAGS"


class TestTagKind:
    def test_sets_the_ses_message_tag_header(self):
        message = mail.EmailMessage(
            "subject", "body", "from@example.com", ["to@example.com"]
        )

        tag_kind(message, EmailKind.COACH_INVITE)

        assert message.extra_headers[TAG_HEADER] == "kind=coach_invite"


class TestKindFromHeaders:
    def test_recovers_the_tagged_kind(self):
        assert (
            kind_from_headers({TAG_HEADER: "kind=margin_alert"})
            == EmailKind.MARGIN_ALERT
        )

    def test_missing_header_is_other(self):
        assert kind_from_headers({}) == EmailKind.OTHER

    def test_unrecognised_value_is_other(self):
        assert kind_from_headers({TAG_HEADER: "kind=nonsense"}) == EmailKind.OTHER


class TestKindFromTags:
    def test_recovers_the_tagged_kind(self):
        assert (
            kind_from_tags({"kind": ["block_delivered"]}) == EmailKind.BLOCK_DELIVERED
        )

    def test_missing_tag_is_other(self):
        assert (
            kind_from_tags({"ses:configuration-set": ["Tracking"]}) == EmailKind.OTHER
        )

    def test_empty_tags_is_other(self):
        assert kind_from_tags({}) == EmailKind.OTHER

    def test_unrecognised_value_is_other(self):
        assert kind_from_tags({"kind": ["nonsense"]}) == EmailKind.OTHER


class TestSendersTagTheirMessages:
    """Every sender in emails.py stamps the right EmailKind on its way out."""

    def test_send_contact_emails_tags_owner_and_ack(self):
        send_contact_emails("Question", "Hello there", "visitor@example.com")

        assert len(mail.outbox) == 2
        owner, ack = mail.outbox
        assert owner.extra_headers[TAG_HEADER] == "kind=contact_owner"
        assert ack.extra_headers[TAG_HEADER] == "kind=contact_ack"

    def test_send_coach_invite_email_tags_coach_invite(self):
        coach = UserFactory()

        send_coach_invite_email(
            coach=coach, email="athlete@example.com", accept_url="https://x.test/claim/"
        )

        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=coach_invite"

    def test_send_coach_invite_reminder_email_tags_invite_reminder(self):
        coach = UserFactory()

        send_coach_invite_reminder_email(
            coach=coach, email="athlete@example.com", accept_url="https://x.test/claim/"
        )

        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=invite_reminder"

    def test_send_coach_request_email_tags_coach_request(self):
        athlete = UserFactory()
        coach = UserFactory(email="coach@example.com")

        send_coach_request_email(
            athlete=athlete, coach=coach, roster_url="https://x.test/meso/"
        )

        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=coach_request"

    def test_send_margin_alert_email_tags_margin_alert(self, settings):
        settings.ADMINS = [("Lance Goyke", "lance@lancegoyke.com")]
        alert = type(
            "Alert",
            (),
            {
                "label": "Coach X",
                "billing_status": "active",
                "billable_seats": 1,
                "totals": type("Totals", (), {"runs": 1, "cost": 1})(),
                "revenue": 2,
                "margin": 1,
                "cost_to_revenue_ratio": 0.5,
            },
        )()

        send_margin_alert_email(alerts=[alert], month_label="2026-06", threshold=0.5)

        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=margin_alert"

    def test_send_block_delivered_email_tags_block_delivered(self):
        athlete = UserFactory()
        coach = UserFactory()
        plan = type("Plan", (), {"title": "Hypertrophy Block"})()

        send_block_delivered_email(
            athlete=athlete,
            coach=coach,
            plan=plan,
            week_count=2,
            home_url="https://x.test/meso/me/",
        )

        assert mail.outbox[0].extra_headers[TAG_HEADER] == "kind=block_delivered"
