"""Day-label editing in the Meso designer (#620, commit 3 of 3)."""

import json
from unittest import mock

import pytest
from django.urls import reverse

from store_project.meso import history
from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import PlanAction
from store_project.meso.models import SessionSlot
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_plan():
    relationship = CoachAthleteFactory(
        coach=UserFactory(name="Coach Maya", email="coach@example.com"),
        athlete=UserFactory(name="Jordan", email="jordan@example.com"),
    )
    plan = relationship.create_plan()
    slot = plan.mesocycles.get().session_slots.order_by("order").first()
    session = slot.sessions.get()
    return plan, slot, session


def name_url(plan_id, slot_id):
    return reverse(
        "meso:api_session_slot_name",
        kwargs={"plan_id": plan_id, "slot_id": slot_id},
    )


def post_name(client, plan, slot, name):
    return client.post(
        name_url(plan.pk, slot.pk),
        data=json.dumps({"name": name}),
        content_type="application/json",
    )


class TestSessionSlotNamePatch:
    def test_round_trip_trims_and_surfaces_in_the_grid_payload(self, client):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)

        response = post_name(client, plan, slot, "  Heavy lower  ")

        assert response.status_code == 200
        assert response.json()["day"] == {
            "session_slot_id": slot.pk,
            "name": "Heavy lower",
            "day_number": 1,
        }
        assert response.json()["history"]["can_undo"] is True
        slot.refresh_from_db()
        assert slot.name == "Heavy lower"

        grid = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})
        )
        assert grid.status_code == 200
        assert grid.json()["days"][0]["name"] == "Heavy lower"

    @pytest.mark.parametrize("payload", [{}, {"name": 17}])
    def test_non_string_validation_returns_the_designer_400_shape(
        self, client, payload
    ):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)

        response = client.post(
            name_url(plan.pk, slot.pk),
            data=json.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json() == {
            "ok": False,
            "error": "name must be a string.",
        }
        assert not PlanAction.objects.filter(plan=plan).exists()

    def test_name_over_255_characters_is_rejected(self, client):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)

        response = post_name(client, plan, slot, "x" * 256)

        assert response.status_code == 400
        assert response.json() == {"ok": False, "error": "name is too long."}
        slot.refresh_from_db()
        assert slot.name == "Day 1"

    def test_blank_name_is_allowed_and_trimmed(self, client):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)

        response = post_name(client, plan, slot, " \n\t ")

        assert response.status_code == 200
        assert response.json()["day"]["name"] == ""
        slot.refresh_from_db()
        assert slot.name == ""

    def test_non_editors_foreign_and_soft_deleted_slots_are_rejected(self, client):
        plan, slot, _session = seed_plan()
        other_coach = CoachAthleteFactory().coach
        client.force_login(other_coach)
        assert post_name(client, plan, slot, "Not yours").status_code == 403

        client.force_login(plan.coach)
        other_plan, foreign_slot, _ = seed_plan()
        assert post_name(client, plan, foreign_slot, "Wrong plan").status_code == 404
        assert (
            client.post(
                name_url(plan.pk, 999999),
                data=json.dumps({"name": "Missing"}),
                content_type="application/json",
            ).status_code
            == 404
        )

        slot.soft_delete()
        assert post_name(client, plan, slot, "Deleted").status_code == 404

        client.force_login(plan.athlete)
        assert post_name(client, other_plan, foreign_slot, "Athlete").status_code == 403

    def test_rename_records_then_undo_and_redo_restore_each_value(self, client):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)

        assert post_name(client, plan, slot, "Power day").status_code == 200

        action = PlanAction.objects.get(plan=plan, stack=PlanAction.Stack.UNDO)
        assert action.label == "Renamed day"
        saved_slot = next(
            row for row in action.snapshot["session_slots"] if row["pk"] == slot.pk
        )
        assert saved_slot["name"] == "Day 1"

        undo = client.post(
            reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk}),
            data=json.dumps({}),
            content_type="application/json",
        )
        assert undo.status_code == 200
        slot.refresh_from_db()
        assert slot.name == "Day 1"

        redo = client.post(
            reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk}),
            data=json.dumps({}),
            content_type="application/json",
        )
        assert redo.status_code == 200
        slot.refresh_from_db()
        assert slot.name == "Power day"

    def test_unchanged_trimmed_name_does_not_record_or_touch_the_plan(self, client):
        plan, slot, _session = seed_plan()
        original_modified = plan.modified
        client.force_login(plan.coach)

        response = post_name(client, plan, slot, "  Day 1  ")

        assert response.status_code == 200
        assert response.json()["history"]["can_undo"] is False
        assert not PlanAction.objects.filter(plan=plan).exists()
        plan.refresh_from_db()
        assert plan.modified == original_modified

    def test_records_before_mutating_and_takes_the_plan_lock(self, client):
        plan, slot, _session = seed_plan()
        client.force_login(plan.coach)
        name_seen_by_recorder = []
        real_record = views.record_plan_action

        def record_before_write(recorded_plan, label):
            name_seen_by_recorder.append(SessionSlot.objects.get(pk=slot.pk).name)
            return real_record(recorded_plan, label)

        with (
            mock.patch.object(
                history.models.Plan.objects,
                "select_for_update",
                wraps=history.models.Plan.objects.select_for_update,
            ) as select_for_update,
            mock.patch.object(
                views, "record_plan_action", side_effect=record_before_write
            ) as recorder,
        ):
            response = post_name(client, plan, slot, "Locked rename")

        assert response.status_code == 200
        recorder.assert_called_once_with(plan, "Renamed day")
        select_for_update.assert_called_once_with(no_key=True)
        assert name_seen_by_recorder == ["Day 1"]


def test_renamed_and_blank_days_reach_athlete_surfaces(client):
    plan, slot, session = seed_plan()
    client.force_login(plan.coach)
    assert post_name(client, plan, slot, "Speed day").status_code == 200

    client.force_login(plan.athlete)
    home = client.get(reverse("meso:athlete_home")).content.decode()
    detail = client.get(
        reverse("meso:athlete_session", kwargs={"pk": session.pk})
    ).content.decode()
    assert "Speed day" in home
    assert "Speed day" in detail

    client.force_login(plan.coach)
    assert post_name(client, plan, slot, "").status_code == 200
    client.force_login(plan.athlete)
    blank_home = client.get(reverse("meso:athlete_home")).content.decode()
    blank_detail = client.get(
        reverse("meso:athlete_session", kwargs={"pk": session.pk})
    ).content.decode()
    assert "Day 1" in blank_home
    assert "Day 1" in blank_detail
    assert presenters.athlete_session(session, plan.athlete)["name"] == "Day 1"


def test_delivery_diff_label_falls_back_for_a_blank_day_name():
    assert serializers._session_label({"n": 4, "name": ""}) == "Day 4"
