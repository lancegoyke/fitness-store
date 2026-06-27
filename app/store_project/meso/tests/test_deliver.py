"""Phase 4 — deliver (lightweight).

Delivering a plan stamps its current week's ``delivered_at`` and records a
``WeekDelivery`` snapshot of that week, behind the same ownership checks as the
other autosave endpoints. The deliver *screen* gains a real, plan-bound mode
(``/meso/deliver/<plan_id>/``) whose button hits that endpoint; the no-arg URL
stays on fixtures until Phase 5.

These tests cover the server seam:

- ``POST api/plan/<id>/deliver/`` stamps the current week + writes a snapshot;
- the snapshot payload captures the week's sessions and prescriptions;
- re-delivering records a fresh snapshot without losing the prior one;
- every call enforces ``request.user == plan.relationship.coach`` over an
  *active* relationship (non-owner / inactive → 403);
- the plan-bound deliver screen renders real data and 404s a non-owned plan.
"""

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
    presc = ExercisePrescriptionFactory(
        session=session, name="Box Squat", sets="4", reps="6", load="70", rpe="7"
    )
    return plan, week, session, presc


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


class TestDeliver:
    def test_deliver_stamps_current_week_and_records_snapshot(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        assert week.delivered_at is None

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        week.refresh_from_db()
        assert week.delivered_at is not None
        delivery = WeekDelivery.objects.get(week=week)
        assert delivery.delivered_at == week.delivered_at

    def test_snapshot_captures_sessions_and_prescriptions(self, client):
        plan, week, session, presc = seed_plan()
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))

        payload = WeekDelivery.objects.get(week=week).payload
        assert payload["week"]["index"] == 1
        sessions = payload["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["name"] == "Lower"
        exercises = sessions[0]["exercises"]
        assert any(e["name"] == "Box Squat" and e["load"] == "70" for e in exercises)

    def test_deliver_stamps_flagged_current_week_only(self, client):
        plan, week1, _, _ = seed_plan()
        # A second, current week in the same plan; week1 is no longer current.
        week1.is_current = False
        week1.save(update_fields=["is_current"])
        week2 = WeekFactory(mesocycle=week1.mesocycle, index=2, is_current=True)
        SessionFactory(week=week2, day_number=1, name="Upper")
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week1.delivered_at is None
        assert week2.delivered_at is not None
        assert WeekDelivery.objects.filter(week=week2).count() == 1
        assert WeekDelivery.objects.filter(week=week1).count() == 0

    def test_redelivering_records_a_fresh_snapshot(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))
        client.post(deliver_url(plan))

        assert WeekDelivery.objects.filter(week=week).count() == 2

    def test_response_reports_delivered_week(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        data = client.post(deliver_url(plan)).json()

        assert data["ok"] is True
        assert data["week"]["id"] == week.pk
        assert data["delivered_at"]

    def test_non_owner_deliver_forbidden(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(UserFactory())  # a stranger

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 403
        week.refresh_from_db()
        assert week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    def test_inactive_relationship_deliver_forbidden(self, client):
        plan, week, _, _ = seed_plan()
        coach = plan.relationship.coach
        plan.relationship.end()
        client.force_login(coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 403
        week.refresh_from_db()
        assert week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    def test_deliver_unknown_plan_404(self, client):
        client.force_login(UserFactory())
        resp = client.post(reverse("meso:api_plan_deliver", kwargs={"plan_id": 999999}))
        assert resp.status_code == 404

    def test_deliver_plan_without_weeks_400(self, client):
        rel = CoachAthleteFactory()
        plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 400
        assert not WeekDelivery.objects.exists()

    def test_deliver_requires_login(self, client):
        plan, _, _, _ = seed_plan()
        resp = client.post(deliver_url(plan))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_deliver_get_not_allowed(self, client):
        plan, _, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(deliver_url(plan))
        assert resp.status_code == 405


class TestDeliverScreen:
    def test_plan_screen_renders_real_plan(self, client):
        plan, _, _, _ = seed_plan(athlete=UserFactory(name="Maya Okonkwo"))
        client.force_login(plan.relationship.coach)

        resp = client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))

        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Maya Okonkwo" in body
        assert "Hypertrophy Block" in body
        # The plan id is wired into the page for the real deliver POST.
        assert f"/meso/api/plan/{plan.pk}/deliver/" in body or str(plan.pk) in body

    def test_plan_screen_404_for_non_owned(self, client):
        plan, _, _, _ = seed_plan()
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404

    def test_plan_screen_404_for_inactive_relationship(self, client):
        plan, _, _, _ = seed_plan()
        coach = plan.relationship.coach
        plan.relationship.end()
        client.force_login(coach)
        resp = client.get(reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404
