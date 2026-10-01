"""Block-name editing in the Meso designer (#620, commit 2 of 3)."""

import json
from unittest import mock

import pytest
from django.db import transaction
from django.urls import reverse

from store_project.meso import history
from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.models import Mesocycle
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


def seed_plan():
    relationship = CoachAthleteFactory(
        coach=UserFactory(name="Coach Maya", email="coach@example.com"),
        athlete=UserFactory(name="Jordan", email="jordan@example.com"),
    )
    plan = relationship.create_plan()
    return plan, plan.mesocycles.get()


def name_url(plan_id, mesocycle_id):
    return reverse(
        "meso:api_mesocycle_name",
        kwargs={"plan_id": plan_id, "mesocycle_id": mesocycle_id},
    )


def post_name(client, plan, mesocycle, name):
    return client.post(
        name_url(plan.pk, mesocycle.pk),
        data=json.dumps({"name": name}),
        content_type="application/json",
    )


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def redo_url(plan):
    return reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk})


class TestMesocycleNamePatch:
    def test_round_trip_trims_and_surfaces_in_the_grid_payload(self, client):
        plan, mesocycle = seed_plan()
        client.force_login(plan.coach)

        response = post_name(client, plan, mesocycle, "  Strength block  ")

        assert response.status_code == 200
        assert response.json()["mesocycle"] == {
            "id": mesocycle.pk,
            "name": "Strength block",
        }
        assert response.json()["history"]["can_undo"] is True
        mesocycle.refresh_from_db()
        assert mesocycle.name == "Strength block"

        grid = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})
        )
        assert grid.status_code == 200
        assert grid.json()["mesocycle"]["name"] == "Strength block"
        assert grid.json()["phases"] == [
            {
                "id": mesocycle.pk,
                "name": "Strength block",
                "weeks": "1 wk",
                "state": "current",
            }
        ]

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            ({}, "name must be a string."),
            ({"name": 17}, "name must be a string."),
            ({"name": " \n\t "}, "name is required."),
            ({"name": "x" * 256}, "name is too long."),
        ],
    )
    def test_validation_returns_the_designer_400_shape(self, client, payload, error):
        plan, mesocycle = seed_plan()
        client.force_login(plan.coach)

        response = client.post(
            name_url(plan.pk, mesocycle.pk),
            data=json.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json() == {"ok": False, "error": error}
        mesocycle.refresh_from_db()
        assert mesocycle.name == "Block 1"
        assert not PlanAction.objects.filter(plan=plan).exists()

    def test_non_editors_are_forbidden_and_foreign_blocks_are_not_found(self, client):
        plan, mesocycle = seed_plan()
        other_coach = CoachAthleteFactory().coach
        client.force_login(other_coach)

        assert post_name(client, plan, mesocycle, "Not yours").status_code == 403
        assert (
            client.post(
                name_url(999999, mesocycle.pk),
                data=json.dumps({"name": "Missing"}),
                content_type="application/json",
            ).status_code
            == 404
        )

        client.force_login(plan.coach)
        other_plan = CoachAthleteFactory(coach=plan.coach).create_plan()
        foreign_mesocycle = other_plan.mesocycles.get()
        assert (
            post_name(client, plan, foreign_mesocycle, "Wrong plan").status_code == 404
        )
        assert (
            client.post(
                name_url(plan.pk, 999999),
                data=json.dumps({"name": "Missing"}),
                content_type="application/json",
            ).status_code
            == 404
        )

        client.force_login(plan.athlete)
        assert post_name(client, plan, mesocycle, "Athlete edit").status_code == 403
        mesocycle.refresh_from_db()
        assert mesocycle.name == "Block 1"

    def test_rename_records_old_name_then_undo_and_redo_restore_each_value(
        self, client
    ):
        plan, mesocycle = seed_plan()
        client.force_login(plan.coach)

        response = post_name(client, plan, mesocycle, "Competition block")

        assert response.status_code == 200
        action = PlanAction.objects.get(plan=plan, stack=PlanAction.Stack.UNDO)
        assert action.label == "Renamed block"
        assert action.snapshot["mesocycles"] == [
            {"pk": mesocycle.pk, "name": "Block 1"}
        ]

        undo = client.post(
            undo_url(plan), data=json.dumps({}), content_type="application/json"
        )
        assert undo.status_code == 200
        mesocycle.refresh_from_db()
        assert mesocycle.name == "Block 1"

        redo = client.post(
            redo_url(plan), data=json.dumps({}), content_type="application/json"
        )
        assert redo.status_code == 200
        mesocycle.refresh_from_db()
        assert mesocycle.name == "Competition block"

    def test_unchanged_trimmed_name_does_not_record_or_touch_the_plan(self, client):
        plan, mesocycle = seed_plan()
        original_modified = plan.modified
        client.force_login(plan.coach)

        response = post_name(client, plan, mesocycle, "  Block 1  ")

        assert response.status_code == 200
        assert response.json()["history"]["can_undo"] is False
        assert not PlanAction.objects.filter(plan=plan).exists()
        plan.refresh_from_db()
        assert plan.modified == original_modified

    def test_records_before_mutating_and_takes_the_plan_lock(self, client):
        plan, mesocycle = seed_plan()
        client.force_login(plan.coach)
        name_seen_by_recorder = []
        real_record = views.record_plan_action

        def record_before_write(recorded_plan, label):
            name_seen_by_recorder.append(Mesocycle.objects.get(pk=mesocycle.pk).name)
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
            response = post_name(client, plan, mesocycle, "Locked rename")

        assert response.status_code == 200
        recorder.assert_called_once_with(plan, "Renamed block")
        select_for_update.assert_called_once_with(no_key=True)
        assert name_seen_by_recorder == ["Block 1"]


def test_snapshot_mesocycle_restore_tolerates_older_snapshots():
    plan, mesocycle = seed_plan()
    snapshot = history.serialize_plan_snapshot(plan)
    assert snapshot["mesocycles"] == [{"pk": mesocycle.pk, "name": "Block 1"}]

    old_snapshot = dict(snapshot)
    old_snapshot.pop("mesocycles")
    mesocycle.name = "Keep this block name"
    mesocycle.save(update_fields=["name"])

    with transaction.atomic():
        locked_plan = Plan.objects.select_for_update().get(pk=plan.pk)
        history.restore_plan_snapshot(locked_plan, old_snapshot)

    mesocycle.refresh_from_db()
    assert mesocycle.name == "Keep this block name"


def test_snapshot_mesocycle_restore_skips_missing_and_foreign_pks():
    plan, mesocycle = seed_plan()
    missing = MesocycleFactory(plan=plan, name="Temporary", order=1)
    foreign = MesocycleFactory(name="Foreign block")
    snapshot = history.serialize_plan_snapshot(plan)
    snapshot["mesocycles"].append({"pk": foreign.pk, "name": "Hijacked"})

    missing.delete()
    mesocycle.name = "Changed block"
    mesocycle.save(update_fields=["name"])

    with transaction.atomic():
        locked_plan = Plan.objects.select_for_update().get(pk=plan.pk)
        history.restore_plan_snapshot(locked_plan, snapshot)

    mesocycle.refresh_from_db()
    foreign.refresh_from_db()
    assert mesocycle.name == "Block 1"
    assert foreign.name == "Foreign block"


def test_athlete_home_card_reads_the_renamed_block(client):
    plan, mesocycle = seed_plan()
    client.force_login(plan.coach)
    assert post_name(client, plan, mesocycle, "Competition block").status_code == 200

    client.force_login(plan.athlete)
    body = client.get(reverse("meso:athlete_home")).content.decode()

    assert "Competition block" in body
    assert "Block 1" not in body
