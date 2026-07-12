"""Phase 3 — designer save/load.

The designer (``static/js/meso.js``) stops owning fixtures: the view serializes a
real ``Plan`` into the page and the JS hydrates from it, then autosaves edits to
ownership-checked JSON endpoints. These tests cover the server seam:

- ``meso:designer_plan`` renders a real plan's serialized JSON into the page
  (and 404s a plan the requester does not coach);
- patching a prescription cell persists and survives a reload;
- adding an exercise to a session persists;
- every autosave endpoint enforces ``request.user == plan.relationship.coach``
  over an *active* relationship (non-owner / inactive → 403).
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import Plan
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc

pytestmark = pytest.mark.django_db


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan with one current week → session → prescription cell."""
    rel = CoachAthleteFactory(
        coach=coach or UserFactory(), athlete=athlete or UserFactory()
    )
    plan = PlanFactory(
        relationship=rel, title="Hypertrophy Block", status=Plan.Status.ACTIVE
    )
    meso = MesocycleFactory(plan=plan, name="Hypertrophy", order=0)
    week = WeekFactory(mesocycle=meso, index=1, is_current=True)
    session = day(week, day_number=1, name="Lower")
    cell = presc(session, name="Box Squat", sets="4", reps="6", load="70", rpe="7")
    return plan, session, cell


class TestDesignerLoad:
    def test_designer_plan_serializes_real_plan(self, client):
        plan, _, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 200
        body = resp.content.decode()
        # Issue #455 phase A5: the island hydrates from #meso-grid-data alone
        # now (useGrid is the sole data owner) — serialize_mesocycle_grid's
        # plan/days additions (step 1) carry the same identifying substrings
        # #meso-plan-data used to.
        assert 'id="meso-grid-data"' in body
        assert "Hypertrophy Block" in body
        assert "Box Squat" in body

    def test_designer_plan_404_for_non_owned(self, client):
        plan, _, _ = seed_plan()
        client.force_login(UserFactory())  # not this plan's coach
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404

    def test_designer_plan_404_for_inactive_relationship(self, client):
        plan, _, _ = seed_plan()
        coach = plan.relationship.coach
        plan.relationship.end()  # relationship no longer active
        client.force_login(coach)
        resp = client.get(reverse("meso:designer_plan", kwargs={"plan_id": plan.pk}))
        assert resp.status_code == 404

    def test_bare_designer_redirects_when_no_plan(self, client):
        # Phase 5 retired the client-side fixtures: the bare URL no longer
        # renders — it redirects to the coach's working plan (or the roster).
        client.force_login(UserFactory())
        resp = client.get(reverse("meso:designer"))
        assert resp.status_code == 302
        assert resp.url == reverse("meso:roster")


class TestPrescriptionPatch:
    def _url(self, plan, presc):
        return reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": plan.pk, "pk": presc.pk},
        )

    def test_patch_persists_and_survives_reload(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "75", "rpe": "8", "note": "felt easy"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        presc.refresh_from_db()
        assert presc.load == "75"
        assert presc.rpe == "8"
        assert presc.note == "felt easy"
        # Survives reload: the serialized designer reflects the saved value.
        reload_body = client.get(
            reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        ).content.decode()
        assert "felt easy" in reload_body

    def test_patch_only_touches_provided_fields(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "80"}),
            content_type="application/json",
        )
        presc.refresh_from_db()
        assert presc.load == "80"
        assert presc.sets == "4"  # untouched
        assert presc.reps == "6"  # untouched

    def test_non_owner_patch_forbidden(self, client):
        plan, _, presc = seed_plan()
        client.force_login(UserFactory())  # a stranger
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "999"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        presc.refresh_from_db()
        assert presc.load == "70"  # unchanged

    def test_patch_inactive_relationship_forbidden(self, client):
        plan, _, presc = seed_plan()
        coach = plan.relationship.coach
        plan.relationship.end()
        client.force_login(coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "999"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        presc.refresh_from_db()
        assert presc.load == "70"

    def test_patch_rejects_foreign_prescription(self, client):
        plan, _, _ = seed_plan()
        # A prescription that belongs to a *different* plan.
        _, _, other_presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, other_presc),
            data=json.dumps({"load": "5"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_patch_rejects_overlong_value(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "x" * 40}),  # load max_length is 32
            content_type="application/json",
        )
        assert resp.status_code == 400
        presc.refresh_from_db()
        assert presc.load == "70"

    def test_patch_rejects_non_string_value(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"sets": 4}),  # must be a string
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_patch_rejects_malformed_json(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc), data="not json", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_patch_requires_login(self, client):
        plan, _, presc = seed_plan()
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"load": "75"}),
            content_type="application/json",
        )
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_patch_get_not_allowed(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(self._url(plan, presc))
        assert resp.status_code == 405


class TestAddExercise:
    def _url(self, plan, session):
        return reverse(
            "meso:api_session_add_exercise",
            kwargs={"plan_id": plan.pk, "pk": session.pk},
        )

    def test_add_exercise_persists(self, client):
        plan, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        before = session.cells().count()
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 201
        assert session.cells().count() == before + 1
        payload = resp.json()["prescription"]
        assert isinstance(payload["id"], int)
        assert payload["name"] == "New exercise"
        # The new row lands after the existing ones (max order + 1).
        added = session.cells().last()
        assert added.pk == payload["id"]
        assert added.exercise_slot.order == cell.exercise_slot.order + 1

    def test_non_owner_add_forbidden(self, client):
        plan, session, _ = seed_plan()
        client.force_login(UserFactory())
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 403
        assert session.cells().count() == 1

    def test_add_rejects_foreign_session(self, client):
        plan, _, _ = seed_plan()
        _, other_session, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(self._url(plan, other_session))
        assert resp.status_code == 404

    def test_add_requires_login(self, client):
        plan, session, _ = seed_plan()
        resp = client.post(self._url(plan, session))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_add_get_not_allowed(self, client):
        plan, session, _ = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.get(self._url(plan, session))
        assert resp.status_code == 405


class TestNamePatchIsSwapAware:
    """``name`` in a cell patch resolves to the right identity (P0 fixed lineup).

    A normal cell's rename edits the block-shared ``ExerciseSlot`` (a fixed-lineup
    rename); a swapped cell's rename edits only that week's swap. Crucially, the
    React client echoes the cell's *effective* name on every autosave (even a
    sets-only edit), so an unchanged name must be a no-op — otherwise a swapped
    cell's routine autosave would rename the base row for the whole block.
    """

    def _patch(self, client, plan, cell, body):
        return client.post(
            reverse(
                "meso:api_prescription_patch",
                kwargs={"plan_id": plan.pk, "pk": cell.pk},
            ),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_rename_a_normal_cell_renames_the_block_slot(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Front Squat"})

        assert resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Front Squat"  # block-wide rename

    def test_editing_a_swapped_cells_name_retargets_the_swap(self, client):
        plan, _, cell = seed_plan()
        cell.swap_name = "Goblet Squat"
        cell.save(update_fields=["swap_name"])
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Hack Squat", "sets": "5"})

        assert resp.status_code == 200
        cell.refresh_from_db()
        cell.exercise_slot.refresh_from_db()
        assert cell.swap_name == "Hack Squat"  # this week's swap changed
        assert cell.exercise_slot.name == "Box Squat"  # base row untouched
        assert cell.sets == "5"

    def test_swapped_cells_unchanged_name_autosave_leaves_the_slot_alone(self, client):
        # A sets-only edit echoes the shown (swap) name unchanged — it must NOT
        # rename the block base row (the whole point of the guard).
        plan, _, cell = seed_plan()
        cell.swap_name = "Goblet Squat"
        cell.save(update_fields=["swap_name"])
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Goblet Squat", "sets": "3"})

        assert resp.status_code == 200
        cell.refresh_from_db()
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Box Squat"  # unchanged
        assert cell.swap_name == "Goblet Squat"  # unchanged
        assert cell.sets == "3"
