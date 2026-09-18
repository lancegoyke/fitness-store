"""The notifications admin ledgers are undeletable (issue #507, part 1).

``SentEmail`` and ``EmailEvent`` are system-written (``notifications.ses_events``,
off SES/SNS webhook events and the ``message_sent`` signal) — ``SentEmailAdmin``
and ``EmailEventAdmin`` already block adding rows and make every field
read-only. But a staff user with the model's Django ``delete`` permission
could still remove a row via the change-page Delete button or the changelist's
bulk ``delete_selected`` action, destroying part of the deliverability record.
This file proves both admin classes refuse deletion (and editing) outright,
while GET access — the whole point of a read-only ledger — still works.
"""

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.notifications.models import EmailEvent
from store_project.notifications.models import EmailKind
from store_project.notifications.models import SentEmail
from store_project.users.factories import SuperAdminFactory

pytestmark = pytest.mark.django_db


def _sent():
    return SentEmail.objects.create(
        ses_message_id="ses-admin-test",
        kind=EmailKind.OTHER,
        recipient="athlete@example.com",
        subject="Hi",
    )


def _event():
    return EmailEvent.objects.create(
        event_type=EmailEvent.EventType.OPEN,
        ses_message_id="ses-admin-test",
        sns_message_id="sns-admin-test",
        recipient="athlete@example.com",
        kind=EmailKind.OTHER,
        occurred_at=timezone.now(),
    )


@pytest.fixture
def superuser_client(client):
    client.force_login(SuperAdminFactory())
    return client


class TestViewAccessStillWorks:
    """Read-only access is the whole point — GET must still return 200."""

    def test_sent_email_changelist_and_change_page(self, superuser_client):
        sent = _sent()

        changelist = superuser_client.get(
            reverse("admin:notifications_sentemail_changelist")
        )
        change = superuser_client.get(
            reverse("admin:notifications_sentemail_change", args=[sent.pk])
        )

        assert changelist.status_code == 200
        assert change.status_code == 200

    def test_email_event_changelist_and_change_page(self, superuser_client):
        event = _event()

        changelist = superuser_client.get(
            reverse("admin:notifications_emailevent_changelist")
        )
        change = superuser_client.get(
            reverse("admin:notifications_emailevent_change", args=[event.pk])
        )

        assert changelist.status_code == 200
        assert change.status_code == 200


class TestDeleteViewIsForbidden:
    """The change-page Delete button (and the confirm POST behind it) is blocked."""

    def test_sent_email_delete_confirmation_is_forbidden(self, superuser_client):
        sent = _sent()

        response = superuser_client.post(
            reverse("admin:notifications_sentemail_delete", args=[sent.pk]),
            {"post": "yes"},
        )

        assert response.status_code == 403
        assert SentEmail.objects.filter(pk=sent.pk).exists()

    def test_email_event_delete_confirmation_is_forbidden(self, superuser_client):
        event = _event()

        response = superuser_client.post(
            reverse("admin:notifications_emailevent_delete", args=[event.pk]),
            {"post": "yes"},
        )

        assert response.status_code == 403
        assert EmailEvent.objects.filter(pk=event.pk).exists()


class TestBulkDeleteActionDoesNothing:
    """The changelist's bulk ``delete_selected`` action can't remove rows either.

    ``delete_selected`` is only offered to a ModelAdmin whose
    ``has_delete_permission`` is true, so it drops out of the action
    choices entirely and posting it (with the ``post=yes`` confirmation a
    real user submission would include) fails the action form's validation
    instead — the changelist just re-renders with nothing deleted.
    """

    def test_sent_email_bulk_delete_leaves_the_row(self, superuser_client):
        sent = _sent()

        response = superuser_client.post(
            reverse("admin:notifications_sentemail_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [str(sent.pk)],
                "index": "0",
                "post": "yes",
            },
        )

        assert response.status_code == 200
        assert SentEmail.objects.filter(pk=sent.pk).exists()

    def test_email_event_bulk_delete_leaves_the_row(self, superuser_client):
        event = _event()

        response = superuser_client.post(
            reverse("admin:notifications_emailevent_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [str(event.pk)],
                "index": "0",
                "post": "yes",
            },
        )

        assert response.status_code == 200
        assert EmailEvent.objects.filter(pk=event.pk).exists()
