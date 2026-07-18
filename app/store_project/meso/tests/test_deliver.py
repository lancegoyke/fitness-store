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
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import WeekDelivery
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc as presc_

pytestmark = pytest.mark.django_db


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one (earliest-live) week → session → prescription."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1)
    session = day(week, day_number=1, name="Lower")
    presc = presc_(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, week, session, presc


def add_week(plan, *, index):
    """Append another week (with one new day) to the plan's first block."""
    meso = plan.mesocycles.order_by("order").first()
    week = WeekFactory(mesocycle=meso, index=index)
    day(week, day_number=index, name=f"Day {index}")
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
        assert any(
            e["name"] == "Box Squat" and e["text"] == "4 x 6, RPE 7, 70"
            for e in exercises
        )

    def test_deliver_stamps_the_whole_block_not_just_current(self, client):
        # P3 block delivery: sending a plan releases the whole mesocycle, so a
        # second week in the same block is stamped too — not only the target week.
        plan, week1, _, _ = seed_plan()
        week2 = WeekFactory(mesocycle=week1.mesocycle, index=2)
        day(week2, day_number=2, name="Upper")
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week1.delivered_at is not None
        assert week2.delivered_at is not None
        assert WeekDelivery.objects.filter(week=week2).count() == 1
        assert WeekDelivery.objects.filter(week=week1).count() == 1

    def test_redelivering_records_a_fresh_snapshot(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))
        client.post(deliver_url(plan))

        assert WeekDelivery.objects.filter(week=week).count() == 2

    def test_response_reports_delivered_block(self, client):
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        data = client.post(deliver_url(plan)).json()

        assert data["ok"] is True
        assert data["mesocycle"]["id"] == week.mesocycle.pk
        assert data["mesocycle"]["name"] == "Hypertrophy"
        assert data["week_count"] == 1
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
    """Deliver a chosen week via an explicit ``week_id``.

    The multi-week designer lets a coach build weeks ahead; this lets them
    *send* a built-ahead week directly, without it being the plan's default
    (earliest-live) week. The athlete sees the newest-delivered week, and a
    foreign week is rejected.
    """

    def test_choosing_a_week_delivers_its_whole_block(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        resp = post_week(client, plan, week2.pk)

        assert resp.status_code == 201
        assert resp.json()["mesocycle"]["id"] == week2.mesocycle.pk
        assert resp.json()["week_count"] == 2
        week1.refresh_from_db()
        week2.refresh_from_db()
        # Choosing any week delivers its whole mesocycle — both weeks are stamped
        # + snapshotted, not just the one named in the body.
        assert week2.delivered_at is not None
        assert week1.delivered_at is not None
        assert WeekDelivery.objects.filter(week=week2).count() == 1
        assert WeekDelivery.objects.filter(week=week1).count() == 1

    def test_delivering_a_block_stamps_every_live_week(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        post_week(client, plan, week2.pk)

        # The whole block is nudged about at once: every live week shares one
        # ``delivered_at`` notify marker (2d — a heads-up stamp, not a gate).
        week1.refresh_from_db()
        week2.refresh_from_db()
        assert week1.delivered_at is not None
        assert week1.delivered_at == week2.delivered_at

    def test_foreign_week_id_is_404_and_delivers_nothing(self, client):
        plan, _, _, _ = seed_plan()
        other_plan, other_week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_week(client, plan, other_week.pk)

        assert resp.status_code == 404
        other_week.refresh_from_db()
        assert other_week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    @pytest.mark.parametrize(
        "bad_value",
        [
            "not-a-number",  # string → not an int-pk
            1.9,  # float → must not coerce to 1
            True,  # bool → must not coerce to 1
            [1],  # wrong type entirely
        ],
    )
    def test_non_integer_week_id_is_a_clean_400(self, client, bad_value):
        # ``week_id`` arrives from JSON; only a genuine integer is honored. A
        # string would 500 the int-pk query; a float/bool would silently coerce
        # onto a valid pk and act on the wrong week — both must answer 400.
        plan, week, _, _ = seed_plan()
        # A week whose pk could be the coercion target (1) of 1.9 / True.
        add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        resp = client.post(
            deliver_url(plan),
            data=json.dumps({"week_id": bad_value}),
            content_type="application/json",
        )

        assert resp.status_code == 400
        week.refresh_from_db()
        assert week.delivered_at is None
        assert not WeekDelivery.objects.exists()

    def test_malformed_json_body_is_a_400_not_a_silent_current_delivery(self, client):
        # A truncated / tampered body that meant to pin a week must fail loudly,
        # not silently stamp + email/push the live week.
        plan, week, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.post(
            deliver_url(plan),
            data="{not valid json",
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
        week2 = add_week(plan, index=2)
        client.force_login(coach)

        resp = post_week(client, plan, week2.pk)

        assert resp.status_code == 402
        week2.refresh_from_db()
        assert week2.delivered_at is None


class TestBlockDeliver:
    """P3: the individual deliver path releases the whole block (one mesocycle).

    Delivering stamps ``delivered_at`` + writes a ``WeekDelivery`` snapshot for
    every *live* week of the target week's mesocycle at once — the athlete gets
    the whole block in a single release, not one week at a time.
    """

    def test_delivers_every_live_week_in_the_block(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        week3 = add_week(plan, index=3)
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        assert resp.json()["week_count"] == 3
        for w in (week1, week2, week3):
            w.refresh_from_db()
            assert w.delivered_at is not None
        # One shared timestamp across the whole block.
        assert week1.delivered_at == week2.delivered_at == week3.delivered_at

    def test_one_delivery_row_per_live_week(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))

        assert WeekDelivery.objects.filter(week=week1).count() == 1
        assert WeekDelivery.objects.filter(week=week2).count() == 1
        assert WeekDelivery.objects.count() == 2

    def test_redeliver_restamps_and_adds_fresh_rows(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        client.post(deliver_url(plan))
        week1.refresh_from_db()
        first_stamp = week1.delivered_at

        client.post(deliver_url(plan))
        week1.refresh_from_db()

        # Re-delivering re-stamps every week and writes a fresh snapshot each.
        assert week1.delivered_at > first_stamp
        assert WeekDelivery.objects.filter(week=week1).count() == 2
        assert WeekDelivery.objects.filter(week=week2).count() == 2

    def test_soft_deleted_week_is_not_delivered(self, client):
        plan, week1, _, _ = seed_plan()
        dead = add_week(plan, index=2)
        dead.soft_delete()
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        assert resp.json()["week_count"] == 1  # only the live week counts
        dead.refresh_from_db()
        assert dead.delivered_at is None
        assert WeekDelivery.objects.filter(week=dead).count() == 0

    def test_a_week_of_a_different_block_is_not_delivered(self, client):
        # A block == ONE mesocycle. Delivering the target week's block leaves a
        # sibling mesocycle of the same plan untouched.
        plan, week1, _, _ = seed_plan()
        other_meso = MesocycleFactory(plan=plan, name="Strength", order=1)
        other_week = WeekFactory(mesocycle=other_meso, index=1)
        day(other_week, day_number=1, name="Push")
        client.force_login(plan.relationship.coach)

        resp = client.post(deliver_url(plan))

        assert resp.status_code == 201
        assert resp.json()["mesocycle"]["id"] == week1.mesocycle.pk
        week1.refresh_from_db()
        other_week.refresh_from_db()
        assert week1.delivered_at is not None
        assert other_week.delivered_at is None
        assert WeekDelivery.objects.filter(week=other_week).count() == 0


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
        add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        ctx = self._screen(client, plan).context["deliver"]

        # The target week (which *selects the block*) defaults to the earliest
        # live week — there is no flagged "current" week any more, just plain
        # ``(mesocycle.order, index)`` ordering.
        assert ctx["week_id"] == week1.pk

    def test_screen_targets_the_week_query_param_selects_the_block(self, client):
        # ?week= picks which BLOCK to deliver (via one of its weeks); the whole
        # block's live weeks are then listed.
        plan, week1, _, _ = seed_plan()
        meso2 = MesocycleFactory(plan=plan, name="Strength", order=1)
        week2 = WeekFactory(mesocycle=meso2, index=1)
        day(week2, day_number=1, name="Push")
        client.force_login(plan.relationship.coach)

        resp = self._screen(client, plan, week=week2.pk)

        ctx = resp.context["deliver"]
        assert ctx["week_id"] == week2.pk
        assert ctx["block_name"] == "Strength"
        # Only the selected block's weeks — week1 lives in the other block.
        ids = {w["id"] for w in ctx["weeks"]}
        assert ids == {week2.pk}

    def test_screen_lists_every_live_week_in_the_block(self, client):
        plan, week1, _, _ = seed_plan()
        week2 = add_week(plan, index=2)
        client.force_login(plan.relationship.coach)

        resp = self._screen(client, plan)

        deliver = resp.context["deliver"]
        ids = {w["id"] for w in deliver["weeks"]}
        assert ids == {week1.pk, week2.pk}
        assert deliver["week_count"] == 2

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
