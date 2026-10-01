"""Program-title editing in the Meso designer (#620, commit 1 of 3)."""

import json
from unittest import mock

import pytest
from django.db import transaction
from django.urls import reverse

from store_project.meso import history
from store_project.meso import views
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.users.factories import UserFactory

from .test_names import deliver

pytestmark = pytest.mark.django_db


def seed_plan():
    relationship = CoachAthleteFactory(
        coach=UserFactory(name="Coach Maya", email="coach@example.com"),
        athlete=UserFactory(name="Jordan", email="jordan@example.com"),
    )
    return relationship.create_plan()


def title_url(plan_id):
    return reverse("meso:api_plan_title", kwargs={"plan_id": plan_id})


def post_title(client, plan, title):
    return client.post(
        title_url(plan.pk),
        data=json.dumps({"title": title}),
        content_type="application/json",
    )


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def redo_url(plan):
    return reverse("meso:api_plan_redo", kwargs={"plan_id": plan.pk})


class TestPlanTitlePatch:
    def test_round_trip_trims_and_surfaces_in_the_grid_payload(self, client):
        plan = seed_plan()
        client.force_login(plan.coach)

        response = post_title(client, plan, "  Fall strength  ")

        assert response.status_code == 200
        assert response.json()["plan"] == {"title": "Fall strength"}
        assert response.json()["history"]["can_undo"] is True
        plan.refresh_from_db()
        assert plan.title == "Fall strength"

        grid = client.get(
            reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})
        )
        assert grid.status_code == 200
        assert grid.json()["plan"]["title"] == "Fall strength"

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            ({}, "title must be a string."),
            ({"title": 17}, "title must be a string."),
            ({"title": " \n\t "}, "title is required."),
            ({"title": "x" * 256}, "title is too long."),
        ],
    )
    def test_validation_returns_the_designer_400_shape(self, client, payload, error):
        plan = seed_plan()
        client.force_login(plan.coach)

        response = client.post(
            title_url(plan.pk),
            data=json.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.json() == {"ok": False, "error": error}
        plan.refresh_from_db()
        assert plan.title == "New program"
        assert not PlanAction.objects.filter(plan=plan).exists()

    def test_non_editors_are_forbidden_and_unknown_plans_are_not_found(self, client):
        plan = seed_plan()
        other_coach = CoachAthleteFactory().coach
        client.force_login(other_coach)

        assert post_title(client, plan, "Not yours").status_code == 403
        assert (
            client.post(
                title_url(999999),
                data=json.dumps({"title": "Missing"}),
                content_type="application/json",
            ).status_code
            == 404
        )

        client.force_login(plan.athlete)
        assert post_title(client, plan, "Athlete edit").status_code == 403
        plan.refresh_from_db()
        assert plan.title == "New program"

    def test_rename_records_old_title_then_undo_and_redo_restore_each_value(
        self, client
    ):
        plan = seed_plan()
        client.force_login(plan.coach)

        response = post_title(client, plan, "Competition prep")

        assert response.status_code == 200
        action = PlanAction.objects.get(plan=plan, stack=PlanAction.Stack.UNDO)
        assert action.label == "Renamed program"
        assert action.snapshot["plan"] == {"title": "New program"}

        undo = client.post(
            undo_url(plan), data=json.dumps({}), content_type="application/json"
        )
        assert undo.status_code == 200
        plan.refresh_from_db()
        assert plan.title == "New program"

        redo = client.post(
            redo_url(plan), data=json.dumps({}), content_type="application/json"
        )
        assert redo.status_code == 200
        plan.refresh_from_db()
        assert plan.title == "Competition prep"

    def test_unchanged_trimmed_title_does_not_record_or_touch_the_plan(self, client):
        plan = seed_plan()
        original_modified = plan.modified
        client.force_login(plan.coach)

        response = post_title(client, plan, "  New program  ")

        assert response.status_code == 200
        assert response.json()["history"]["can_undo"] is False
        assert not PlanAction.objects.filter(plan=plan).exists()
        plan.refresh_from_db()
        assert plan.modified == original_modified

    def test_records_before_mutating_and_takes_the_plan_lock(self, client):
        plan = seed_plan()
        client.force_login(plan.coach)
        title_seen_by_recorder = []
        real_record = views.record_plan_action

        def record_before_write(recorded_plan, label):
            title_seen_by_recorder.append(Plan.objects.get(pk=recorded_plan.pk).title)
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
            response = post_title(client, plan, "Locked rename")

        assert response.status_code == 200
        recorder.assert_called_once_with(plan, "Renamed program")
        select_for_update.assert_called_once_with(no_key=True)
        assert title_seen_by_recorder == ["New program"]


def test_snapshot_title_restore_is_tolerant_of_older_snapshots():
    plan = seed_plan()
    snapshot = history.serialize_plan_snapshot(plan)
    assert snapshot["plan"] == {"title": "New program"}

    old_snapshot = dict(snapshot)
    old_snapshot.pop("plan")
    plan.title = "Keep this title"
    plan.save(update_fields=["title"])

    with transaction.atomic():
        locked_plan = Plan.objects.select_for_update().get(pk=plan.pk)
        history.restore_plan_snapshot(locked_plan, old_snapshot)

    plan.refresh_from_db()
    assert plan.title == "Keep this title"


class TestRenamedTitleOnAthleteSurfaces:
    def test_athlete_home_card_reads_the_renamed_plan_title(self, client):
        plan = seed_plan()
        client.force_login(plan.coach)
        assert post_title(client, plan, "Fall strength").status_code == 200

        client.force_login(plan.athlete)
        body = client.get(reverse("meso:athlete_home")).content.decode()

        assert "Fall strength" in body
        assert "New program" not in body

    def test_delivery_email_reads_the_renamed_plan_title(
        self, client, mailoutbox, django_capture_on_commit_callbacks
    ):
        plan = seed_plan()
        client.force_login(plan.coach)
        assert post_title(client, plan, "Fall strength").status_code == 200

        email = deliver(client, plan, django_capture_on_commit_callbacks)

        assert email is mailoutbox[-1]
        assert "Fall strength" in email.body
        assert "New program" not in email.body
