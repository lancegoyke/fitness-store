"""Meso delivery-email unsubscribe — the email opt-out (deferred follow-up).

The delivered-week email is the one transactional message a coached athlete
receives, and it had no off switch (web push is opt-in via the browser
permission; email was not). This adds the email best-practice — a working,
login-free, one-click ``List-Unsubscribe`` link:

- the delivery email carries ``List-Unsubscribe`` + ``List-Unsubscribe-Post``
  headers and a visible footer link;
- the link carries a signed token naming the athlete (no login, no token column);
- a GET shows a confirm page and never mutates (mail scanners/prefetchers issue
  GETs and must not silently unsubscribe anyone);
- a POST records a single opt-out flag on the athlete's ``AthleteProfile``;
- the deliver hook honors the flag — an opted-out athlete is emailed nothing
  (but push, separately opt-in, still fires) and delivery still succeeds.
"""

from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteProfile
from store_project.meso.models import Plan
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.meso.unsubscribe import athlete_opted_out
from store_project.meso.unsubscribe import make_unsubscribe_token
from store_project.meso.unsubscribe import resolve_unsubscribe_user
from store_project.meso.unsubscribe import set_delivery_email_opt_out
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, week


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


def unsubscribe_url(user):
    return reverse(
        "meso:unsubscribe_delivery_email",
        kwargs={"token": make_unsubscribe_token(user)},
    )


class TestUnsubscribeToken:
    def test_token_round_trips_to_the_user(self):
        user = UserFactory()
        token = make_unsubscribe_token(user)
        assert resolve_unsubscribe_user(token) == user

    def test_tampered_token_resolves_to_none(self):
        user = UserFactory()
        token = make_unsubscribe_token(user)
        assert resolve_unsubscribe_user(token + "x") is None
        assert resolve_unsubscribe_user("not-a-real-token") is None
        assert resolve_unsubscribe_user("") is None

    def test_token_for_one_user_does_not_resolve_to_another(self):
        a, b = UserFactory(), UserFactory()
        assert resolve_unsubscribe_user(make_unsubscribe_token(a)) == a
        assert resolve_unsubscribe_user(make_unsubscribe_token(b)) == b


class TestOptOutHelpers:
    def test_default_is_opted_in(self):
        user = UserFactory()
        assert athlete_opted_out(user) is False

    def test_set_opt_out_creates_profile_when_missing(self):
        user = UserFactory()
        assert not AthleteProfile.objects.filter(user=user).exists()
        set_delivery_email_opt_out(user, True)
        assert athlete_opted_out(user) is True
        profile = AthleteProfile.objects.get(user=user)
        assert profile.delivery_email_opt_out is True

    def test_opt_out_is_reversible(self):
        user = UserFactory()
        set_delivery_email_opt_out(user, True)
        set_delivery_email_opt_out(user, False)
        assert athlete_opted_out(user) is False


class TestUnsubscribeView:
    def test_get_shows_confirm_and_does_not_opt_out(self, client):
        user = UserFactory(email="maya@example.com")
        resp = client.get(unsubscribe_url(user))
        assert resp.status_code == 200
        assert b"maya@example.com" in resp.content
        # A GET must never mutate — scanners/prefetchers issue GETs.
        assert athlete_opted_out(user) is False

    def test_post_opts_the_athlete_out(self, client):
        user = UserFactory(email="maya@example.com")
        resp = client.post(unsubscribe_url(user))
        assert resp.status_code == 200
        assert athlete_opted_out(user) is True

    def test_works_without_login(self):
        # An anonymous visitor (the recipient may not be signed in) can opt out.
        user = UserFactory()
        anon = Client()
        resp = anon.post(unsubscribe_url(user))
        assert resp.status_code == 200
        assert athlete_opted_out(user) is True

    def test_one_click_post_without_csrf_token_succeeds(self):
        # RFC 8058 one-click: the mail client POSTs directly, carrying no CSRF
        # token. The view must be csrf-exempt for that to work.
        user = UserFactory()
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(
            unsubscribe_url(user), data={"List-Unsubscribe": "One-Click"}
        )
        assert resp.status_code == 200
        assert athlete_opted_out(user) is True

    def test_invalid_token_returns_400_and_changes_nothing(self, client):
        bad = reverse(
            "meso:unsubscribe_delivery_email", kwargs={"token": "garbage-token"}
        )
        assert client.get(bad).status_code == 400
        assert client.post(bad).status_code == 400


class TestDeliveryEmailUnsubscribeHeaders:
    def test_delivery_email_carries_list_unsubscribe_headers(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(email="maya@example.com")
        plan, _ = seed_plan(athlete=athlete)
        client.force_login(plan.relationship.coach)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(deliver_url(plan))

        email = mailoutbox[0]
        list_unsub = email.extra_headers["List-Unsubscribe"]
        assert list_unsub.startswith("<http")
        assert list_unsub.endswith(">")
        assert "/meso/unsubscribe/" in list_unsub
        assert (
            email.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        )
        # A visible footer link too, not only a header.
        assert "/meso/unsubscribe/" in email.body

    def test_footer_link_actually_unsubscribes(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(email="maya@example.com")
        plan, _ = seed_plan(athlete=athlete)
        client.force_login(plan.relationship.coach)
        with django_capture_on_commit_callbacks(execute=True):
            client.post(deliver_url(plan))

        # Pull the unsubscribe URL out of the header and follow it (one-click POST).
        list_unsub = mailoutbox[0].extra_headers["List-Unsubscribe"].strip("<>")
        path = list_unsub.split("testserver", 1)[1]
        anon = Client()
        assert anon.post(path).status_code == 200
        assert athlete_opted_out(athlete) is True


class TestDeliveryHonorsOptOut:
    def test_opted_out_athlete_gets_no_email(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(email="maya@example.com")
        plan, week = seed_plan(athlete=athlete)
        set_delivery_email_opt_out(athlete, True)
        client.force_login(plan.relationship.coach)

        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(deliver_url(plan))

        # Delivery still succeeds and the week is stamped; only the email is skipped.
        assert resp.status_code == 201
        assert len(mailoutbox) == 0
        week.refresh_from_db()
        assert week.delivered_at is not None

    def test_opt_out_suppresses_email_but_not_push(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(email="maya@example.com")
        plan, _ = seed_plan(athlete=athlete)
        set_delivery_email_opt_out(athlete, True)
        client.force_login(plan.relationship.coach)

        with (
            mock.patch(
                "store_project.meso.views.meso_push.notify_block_delivered"
            ) as push,
            django_capture_on_commit_callbacks(execute=True),
        ):
            client.post(deliver_url(plan))

        assert len(mailoutbox) == 0  # email opt-out honored
        assert push.call_count == 1  # push is a separate channel, still fired

    def test_opted_in_athlete_still_gets_email(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        athlete = UserFactory(email="maya@example.com")
        plan, _ = seed_plan(athlete=athlete)
        client.force_login(plan.relationship.coach)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(deliver_url(plan))

        assert len(mailoutbox) == 1
        assert mailoutbox[0].to == ["maya@example.com"]
