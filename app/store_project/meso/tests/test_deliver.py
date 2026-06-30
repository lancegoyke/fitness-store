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

import json

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


def add_week(plan, *, index, is_current=False):
    """Append another week (with one session) to the plan's first block."""
    meso = plan.mesocycles.order_by("order").first()
    week = WeekFactory(mesocycle=meso, index=index, is_current=is_current)
    SessionFactory(week=week, day_number=1, name=f"Day {index}")
    return week


def deliver_url(plan):
    return reverse("meso:api_plan_deliver", kwargs={"plan_id": plan.pk})


def post_week(client, plan, week_id):
    """POST the deliver endpoint with an explicit ``week_id`` JSON body."""
    return client.post(
        deliver_url(plan),
        data=json.dumps({"week_id": week_id}),
        content_type="application/json",
    )


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

    def test_empty_json_body_delivers_current_week(self, client):
        # The bare deliver button posts no useful body; an explicit ``{}`` (or no
        # body) must still mean "deliver the live week", not a 400.
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_week(client, plan, None)

        assert resp.status_code == 201
        week.refresh_from_db()
        assert week.delivered_at is not None

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


class TestDeliverChosenWeek:
    """Deliver a chosen (non-current) week via an explicit ``week_id``.

    The multi-week designer lets a coach build weeks ahead; this lets them
    *send* a built-ahead week directly — without first flipping it to the live
    (current) week. The athlete sees the newest-delivered week, the coach's live
    pointer is left untouched, and a foreign week is rejected.
    """

    def test_delivers_the_chosen_non_current_week(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        resp = post_week(client, plan, week2.pk)

        assert resp.status_code == 201
        assert resp.json()["week"]["id"] == week2.pk
        week1.refresh_from_db()
        week2.refresh_from_db()
        # Only the chosen week is delivered + snapshotted; the live week is not.
        assert week2.delivered_at is not None
        assert week1.delivered_at is None
        assert WeekDelivery.objects.filter(week=week2).count() == 1
        assert WeekDelivery.objects.filter(week=week1).count() == 0

    def test_delivering_a_week_does_not_change_the_current_pointer(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        post_week(client, plan, week2.pk)

        week1.refresh_from_db()
        week2.refresh_from_db()
        # Delivering never flips ``is_current`` — week1 stays the live target.
        assert week1.is_current is True
        assert week2.is_current is False

    def test_chosen_week_becomes_the_athletes_visible_week(self, client):
        from store_project.meso.serializers import latest_delivered_week

        plan, _, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        post_week(client, plan, week2.pk)

        # Newest delivery wins → the athlete lands on the week just sent.
        assert latest_delivered_week(plan).pk == week2.pk

    def test_foreign_week_id_is_404_and_delivers_nothing(self, client):
        plan, _, _, _ = seed_plan()
        other_plan, other_week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_week(client, plan, other_week.pk)

        assert resp.status_code == 404
        other_week.refresh_from_db()
        assert other_week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    def test_malformed_week_id_is_a_clean_400_not_500(self, client):
        # ``week_id`` arrives from JSON, so a tampered non-integer must answer a
        # clean 400 — not crash the int-pk query with a 500.
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.post(
            deliver_url(plan),
            data=json.dumps({"week_id": "not-a-number"}),
            content_type="application/json",
        )

        assert resp.status_code == 400
        week.refresh_from_db()
        assert week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    def test_over_limit_coach_cannot_deliver_a_chosen_week(self, client):
        # The D6 freeze guards the endpoint before the body is read, so a
        # per-week deliver is gated exactly like the current-week deliver. A free
        # coach with a kept link + a newer (suspended) one is over the seat cap.
        from store_project.meso.models import CoachAthlete

        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # kept
        plan, _, _, _ = seed_plan(coach=coach)  # newer link → suspended → over
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(coach)

        resp = post_week(client, plan, week2.pk)

        assert resp.status_code == 402
        week2.refresh_from_db()
        assert week2.delivered_at is None


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

    def _screen(self, client, plan, **params):
        url = reverse("meso:deliver_plan", kwargs={"plan_id": plan.pk})
        return client.get(url, params)

    def test_screen_defaults_to_current_week(self, client):
        plan, week1, _, _ = seed_plan()
        add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        ctx = self._screen(client, plan).context["deliver"]

        assert ctx["week_id"] == week1.pk
        assert ctx["is_current"] is True

    def test_screen_targets_the_week_query_param(self, client):
        plan, _, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        resp = self._screen(client, plan, week=week2.pk)

        ctx = resp.context["deliver"]
        assert ctx["week_id"] == week2.pk
        # Targeting a non-current week flags it so the screen can warn the coach.
        assert ctx["is_current"] is False
        assert "Wk 2" in resp.content.decode()

    def test_screen_lists_every_week_for_the_selector(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        resp = self._screen(client, plan)

        weeks = resp.context["deliver"]["weeks"]
        ids = {w["id"] for w in weeks}
        assert ids == {week1.pk, week2.pk}
        body = resp.content.decode()
        # Each week is a link that re-targets the screen at that week.
        assert f"?week={week1.pk}" in body
        assert f"?week={week2.pk}" in body

    def test_screen_warns_when_target_is_not_the_live_week(self, client):
        plan, _, _, _ = seed_plan()
        week2 = add_week(plan, index=2, is_current=False)
        client.force_login(plan.relationship.coach)

        on_current = self._screen(client, plan).content.decode()
        on_other = self._screen(client, plan, week=week2.pk).content.decode()

        # The "not the live week" notice shows only when sending a non-live week.
        assert "live week" not in on_current.lower()
        assert "live week" in on_other.lower()

    def test_screen_foreign_week_param_falls_back_to_current(self, client):
        plan, week1, _, _ = seed_plan()
        _, other_week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        ctx = self._screen(client, plan, week=other_week.pk).context["deliver"]

        # A foreign / stale ?week= silently falls back to the live week (the
        # confirm screen always renders something deliverable); the POST is strict.
        assert ctx["week_id"] == week1.pk

    def test_screen_non_numeric_week_param_falls_back_to_current(self, client):
        plan, week1, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        ctx = self._screen(client, plan, week="nope").context["deliver"]

        assert ctx["week_id"] == week1.pk
