"""Recording a push click on the athlete landing page (#509 slice 3).

``notifications.push.record_push_click`` is wired into
``meso.views.AthleteHomeView.get_context_data`` — the block-delivered push's
only target today (``/meso/me/``). These tests drive it through that real
page rather than calling the helper directly, so the wiring (the view's
import and its GET-only guard) is covered too, not just the helper in
isolation.
"""

import uuid

import pytest
from django.urls import reverse

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.notifications.models import PushKind
from store_project.notifications.models import PushNotification
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

HOME = reverse("meso:athlete_home")


def make_row(user, kind=PushKind.BLOCK_DELIVERED):
    return PushNotification.objects.create(kind=kind, user=user)


class TestLandingClickIsRecordedOnce:
    def test_the_landing_get_sets_clicked_at_once_and_emits_one_event(self, client):
        athlete = UserFactory()
        row = make_row(athlete)
        client.force_login(athlete)

        resp = client.get(HOME, {"n": str(row.pk)})

        assert resp.status_code == 200
        row.refresh_from_db()
        assert row.clicked_at is not None
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 1
        event = Event.objects.get(name=EventName.PUSH_CLICKED)
        assert event.actor == athlete
        assert event.source == Event.Source.SERVER
        assert event.subject_type == "notifications.pushnotification"
        assert event.subject_id == str(row.pk)
        assert event.props.get("kind") == PushKind.BLOCK_DELIVERED

    def test_a_reload_with_the_same_id_changes_nothing_and_emits_nothing_more(
        self, client
    ):
        athlete = UserFactory()
        row = make_row(athlete)
        client.force_login(athlete)

        client.get(HOME, {"n": str(row.pk)})
        row.refresh_from_db()
        first_clicked_at = row.clicked_at
        assert first_clicked_at is not None

        resp = client.get(HOME, {"n": str(row.pk)})

        assert resp.status_code == 200
        row.refresh_from_db()
        assert row.clicked_at == first_clicked_at
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 1

    def test_another_users_id_is_ignored_silently(self, client):
        owner = UserFactory()
        row = make_row(owner)
        stranger = UserFactory()
        client.force_login(stranger)

        resp = client.get(HOME, {"n": str(row.pk)})

        assert resp.status_code == 200
        row.refresh_from_db()
        assert row.clicked_at is None
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 0

    def test_an_unknown_id_is_ignored_silently(self, client):
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.get(HOME, {"n": str(uuid.uuid4())})

        assert resp.status_code == 200
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 0

    def test_a_malformed_id_is_ignored_silently(self, client):
        athlete = UserFactory()
        client.force_login(athlete)

        resp = client.get(HOME, {"n": "not-a-uuid"})

        assert resp.status_code == 200
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 0

    def test_an_anonymous_request_is_unaffected(self, client):
        athlete = UserFactory()
        row = make_row(athlete)

        resp = client.get(HOME, {"n": str(row.pk)})

        # LoginRequiredMixin redirects before get_context_data ever runs.
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url
        row.refresh_from_db()
        assert row.clicked_at is None
        assert Event.objects.filter(name=EventName.PUSH_CLICKED).count() == 0
