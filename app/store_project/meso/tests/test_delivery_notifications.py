"""Athlete slice Phase 4a — delivery notifications (S3).

When a coach delivers a week (``POST api/plan/<id>/deliver/``), the athlete is
emailed that their next training week is ready, with a link to their own
training surface (``/meso/me/``). Email is the channel that exists today
(``django-ses`` + the ``notifications`` app); web push waits on the PWA (4b).

The notification is **best-effort**: delivery has already succeeded by the time
the email is attempted, so a mail failure must never roll it back. And it only
reaches the athlete on a *successful* deliver — the 403/404/400 guard paths send
nothing (they return before the week is stamped).

These tests cover that seam:

- a successful deliver emails the athlete exactly once, at their address;
- the email names the coach, the plan, and the delivered week, and links home;
- an athlete with no email on file is skipped (no crash, delivery still 201);
- a mail backend failure does not break delivery (the week is still stamped);
- only the athlete is emailed (never the coach);
- re-delivering (a fix-in-place) notifies again;
- the forbidden / unauthenticated guard paths send nothing.
"""

from unittest import mock

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import ExercisePrescriptionFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import WeekDelivery
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
    session = SessionFactory(week=week, day_number=1, name="Lower")
    ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return plan, week


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


class TestDeliveryNotification:
    def test_deliver_emails_the_athlete_once(self, client, mailoutbox):
        coach = UserFactory(name="Coach Lance", email="coach@example.com")
        athlete = UserFactory(name="Maya Okonkwo", email="maya@example.com")
        plan, _ = seed_plan(coach=coach, athlete=athlete)
        client.force_login(coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        assert len(mailoutbox) == 1
        assert mailoutbox[0].to == ["maya@example.com"]

    def test_email_names_coach_plan_and_week(self, client, mailoutbox):
        coach = UserFactory(name="Coach Lance", email="coach@example.com")
        athlete = UserFactory(name="Maya Okonkwo", email="maya@example.com")
        plan, _ = seed_plan(coach=coach, athlete=athlete)
        client.force_login(coach)

        client.post(deliver_url(plan))

        email = mailoutbox[0]
        haystack = f"{email.subject}\n{email.body}"
        assert "Coach Lance" in haystack
        assert "Hypertrophy Block" in haystack
        assert "Week 1" in haystack

    def test_email_links_to_athlete_home(self, client, mailoutbox):
        plan, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))

        body = mailoutbox[0].body
        assert reverse("meso:athlete_home") in body  # /meso/me/
        # An absolute link the athlete can click from their inbox.
        assert "http://testserver" in body

    def test_no_email_when_athlete_has_no_address(self, client, mailoutbox):
        coach = UserFactory(email="coach@example.com")
        athlete = UserFactory(email="")
        plan, week = seed_plan(coach=coach, athlete=athlete)
        client.force_login(coach)

        resp = client.post(deliver_url(plan))

        # Delivery still succeeds; we simply skip the (impossible) email.
        assert resp.status_code == 201
        assert len(mailoutbox) == 0
        week.refresh_from_db()
        assert week.delivered_at is not None

    def test_email_failure_does_not_break_delivery(self, client):
        plan, week = seed_plan()
        client.force_login(plan.relationship.coach)

        with mock.patch(
            "store_project.meso.views.send_week_delivered_email",
            side_effect=RuntimeError("SES is down"),
        ):
            resp = client.post(deliver_url(plan))

        # The mail blew up, but the deliver write committed.
        assert resp.status_code == 201
        week.refresh_from_db()
        assert week.delivered_at is not None
        assert WeekDelivery.objects.filter(week=week).count() == 1

    def test_only_the_athlete_is_emailed(self, client, mailoutbox):
        coach = UserFactory(name="Coach Lance", email="coach@example.com")
        athlete = UserFactory(name="Maya Okonkwo", email="maya@example.com")
        plan, _ = seed_plan(coach=coach, athlete=athlete)
        client.force_login(coach)

        client.post(deliver_url(plan))

        recipients = [addr for email in mailoutbox for addr in email.to]
        assert recipients == ["maya@example.com"]
        assert "coach@example.com" not in recipients

    def test_redelivering_notifies_again(self, client, mailoutbox):
        plan, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))
        client.post(deliver_url(plan))

        assert len(mailoutbox) == 2

    def test_forbidden_deliver_sends_no_email(self, client, mailoutbox):
        plan, _ = seed_plan()
        client.force_login(UserFactory())  # a stranger

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 403
        assert len(mailoutbox) == 0

    def test_unauthenticated_deliver_sends_no_email(self, client, mailoutbox):
        plan, _ = seed_plan()

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 302
        assert len(mailoutbox) == 0
