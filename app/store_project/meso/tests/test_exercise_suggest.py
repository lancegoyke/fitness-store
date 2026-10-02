"""#608 — exercise-name suggestions: the catalog link on a row rename + the payload.

``prescription_patch`` takes an optional ``exercise_id`` (UUID or null) that
links/unlinks the row's ``ExerciseSlot`` to the catalog; a rename with the key
absent unlinks (a free-text rename must not keep a stale FK). The designer page
carries ``serialize_exercise_suggestions`` as a ``meso-exercise-suggest``
json_script.
"""

import json
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from store_project.exercises.factories import ExerciseFactory
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.meso.models import PlanAction
from store_project.meso.serializers import serialize_exercise_suggestions
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def seed(coach=None, *, name="Box Squat", exercise=None):
    rel = CoachAthleteFactory(coach=coach or UserFactory(), athlete=UserFactory())
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE)
    week = WeekFactory(mesocycle=MesocycleFactory(plan=plan, order=0), index=1)
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name=name, exercise=exercise)
    return plan, session, cell


def patch(client, plan, cell, payload):
    url = reverse(
        "meso:api_prescription_patch", kwargs={"plan_id": plan.pk, "pk": cell.pk}
    )
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def actions(plan):
    return PlanAction.objects.filter(plan=plan).count()


class TestPatchLink:
    def test_pick_links(self, client):
        plan, _, cell = seed()
        ex = ExerciseFactory(name="Back Squat")
        client.force_login(plan.relationship.coach)
        resp = patch(
            client, plan, cell, {"name": "Back Squat", "exercise_id": str(ex.pk)}
        )
        assert resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Back Squat"
        assert cell.exercise_slot.exercise_id == ex.pk
        assert actions(plan) == 1

    def test_free_text_rename_unlinks(self, client):
        ex = ExerciseFactory(name="Back Squat")
        plan, _, cell = seed(name="Back Squat", exercise=ex)
        client.force_login(plan.relationship.coach)
        assert (
            patch(client, plan, cell, {"name": "Back Squat (pause)"}).status_code == 200
        )
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Back Squat (pause)"
        assert cell.exercise_slot.exercise_id is None
        assert actions(plan) == 1

    def test_pick_with_same_name_only_links(self, client):
        plan, _, cell = seed(name="Back Squat")
        ex = ExerciseFactory(name="Back Squat")
        client.force_login(plan.relationship.coach)
        patch(client, plan, cell, {"name": "Back Squat", "exercise_id": str(ex.pk)})
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.exercise_id == ex.pk
        assert actions(plan) == 1

    def test_null_unlinks_without_rename(self, client):
        ex = ExerciseFactory()
        plan, _, cell = seed(name="Back Squat", exercise=ex)
        client.force_login(plan.relationship.coach)
        patch(client, plan, cell, {"name": "Back Squat", "exercise_id": None})
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Back Squat"
        assert cell.exercise_slot.exercise_id is None
        assert actions(plan) == 1

    def test_unknown_uuid_400(self, client):
        plan, _, cell = seed()
        client.force_login(plan.relationship.coach)
        resp = patch(client, plan, cell, {"exercise_id": str(uuid.uuid4())})
        assert resp.status_code == 400
        assert b"exercise_id is unknown." in resp.content
        assert actions(plan) == 0

    @pytest.mark.parametrize("bad", ["nope", 5, ["x"], {}, True])
    def test_bad_type_400(self, client, bad):
        plan, _, cell = seed()
        client.force_login(plan.relationship.coach)
        resp = patch(client, plan, cell, {"exercise_id": bad})
        assert resp.status_code == 400
        assert b"UUID string or null" in resp.content
        assert actions(plan) == 0

    def test_unchanged_name_no_key_leaves_link_and_history(self, client):
        ex = ExerciseFactory()
        plan, _, cell = seed(name="Back Squat", exercise=ex)
        client.force_login(plan.relationship.coach)
        assert patch(client, plan, cell, {"name": "Back Squat"}).status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.exercise_id == ex.pk
        assert actions(plan) == 0

    def test_repeat_pick_is_not_a_new_step(self, client):
        plan, _, cell = seed()
        ex = ExerciseFactory(name="Back Squat")
        client.force_login(plan.relationship.coach)
        body = {"name": "Back Squat", "exercise_id": str(ex.pk)}
        patch(client, plan, cell, body)
        patch(client, plan, cell, body)
        assert actions(plan) == 1

    def test_undo_restores_name_and_link(self, client):
        old = ExerciseFactory(name="Box Squat")
        new = ExerciseFactory(name="Back Squat")
        plan, _, cell = seed(name="Box Squat", exercise=old)
        client.force_login(plan.relationship.coach)
        patch(client, plan, cell, {"name": "Back Squat", "exercise_id": str(new.pk)})
        resp = client.post(reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Box Squat"
        assert cell.exercise_slot.exercise_id == old.pk


class TestSuggestionsPayload:
    def test_catalog_sorted_by_name(self):
        b = ExerciseFactory(name="Bench")
        a = ExerciseFactory(name="Arnold Press")
        out = serialize_exercise_suggestions(UserFactory())
        assert out["catalog"] == [
            {"id": str(a.pk), "name": "Arnold Press"},
            {"id": str(b.pk), "name": "Bench"},
        ]
        assert out["mine"] == []

    def test_mine_ordering_dedup_and_exclusions(self):
        coach = UserFactory()
        ex = ExerciseFactory(name="Back Squat")
        plan, session, _ = seed(coach, name="deadlift")
        # "deadlift" x2 total, "Back Squat" linked x1, free-text same name x1.
        presc(session, name="deadlift")
        presc(session, name="Back Squat", exercise=ex)
        presc(session, name="Back Squat")
        presc(session, name="Curl")
        presc(session, name="  ")
        presc(session, name="")
        presc(session, name="New exercise")
        gone = presc(session, name="Gone")
        gone.exercise_slot.soft_delete()
        out = serialize_exercise_suggestions(coach)
        assert out["mine"] == [
            {"name": "deadlift", "exercise_id": None},
            {"name": "Back Squat", "exercise_id": None},
            {"name": "Back Squat", "exercise_id": str(ex.pk)},
            {"name": "Curl", "exercise_id": None},
        ]

    def test_scope_excludes_other_coaches_includes_templates(self):
        coach = UserFactory()
        seed(coach, name="Mine")
        seed(name="Theirs")
        tmpl = PlanFactory(relationship=None, is_template=True, owner=coach)
        week = WeekFactory(mesocycle=MesocycleFactory(plan=tmpl, order=0), index=1)
        presc(day(week, day_number=1), name="From template")
        names = {r["name"] for r in serialize_exercise_suggestions(coach)["mine"]}
        assert names == {"Mine", "From template"}

    def test_soft_deleted_day_excluded(self):
        coach = UserFactory()
        _, session, _ = seed(coach, name="On dead day")
        session.session_slot.soft_delete()
        assert serialize_exercise_suggestions(coach)["mine"] == []


class TestDesignerPage:
    def _get(self, client, plan):
        return client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))

    def test_page_embeds_json_script(self, client):
        ex = ExerciseFactory(name="Back Squat")
        plan, _, _ = seed(name="Box Squat")
        client.force_login(plan.relationship.coach)
        resp = self._get(client, plan)
        assert resp.status_code == 200
        assert 'id="meso-exercise-suggest"' in resp.content.decode()
        data = resp.context["exercise_suggestions"]
        assert data["catalog"] == [{"id": str(ex.pk), "name": "Back Squat"}]
        assert data["mine"] == [{"name": "Box Squat", "exercise_id": None}]

    def test_query_count_does_not_scale_with_rows(self, client):
        plan, session, _ = seed()
        client.force_login(plan.relationship.coach)
        self._get(client, plan)  # warm caches/contenttypes

        def count():
            with CaptureQueriesContext(connection) as ctx:
                self._get(client, plan)
            return len(ctx)

        before = count()
        for i in range(15):
            ExerciseFactory(name=f"Catalog {i}")
            presc(session, name=f"Row {i}")
        assert count() == before
