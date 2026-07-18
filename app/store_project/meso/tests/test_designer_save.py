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
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line

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
    week = WeekFactory(mesocycle=meso, index=1)
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
            data=json.dumps({"text": "4 x 6, RPE 8, 75"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["prescription"]["text"] == "4 x 6, RPE 8, 75"
        presc.refresh_from_db()
        assert presc.text == "4 x 6, RPE 8, 75"
        # Survives reload: the serialized designer reflects the saved value.
        reload_body = client.get(
            reverse("meso:designer_plan", kwargs={"plan_id": plan.pk})
        ).content.decode()
        assert "RPE 8, 75" in reload_body

    def test_patch_only_touches_the_text(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        client.post(
            self._url(plan, presc),
            data=json.dumps({"text": "5 x 5"}),
            content_type="application/json",
        )
        presc.refresh_from_db()
        assert presc.text == "5 x 5"
        assert presc.skipped is False  # untouched
        assert presc.name == "Box Squat"  # slot identity untouched

    def test_non_owner_patch_forbidden(self, client):
        plan, _, presc = seed_plan()
        client.force_login(UserFactory())  # a stranger
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"text": "999"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        presc.refresh_from_db()
        assert presc.text == "4 x 6, RPE 7, 70"  # unchanged

    def test_patch_inactive_relationship_forbidden(self, client):
        plan, _, presc = seed_plan()
        coach = plan.relationship.coach
        plan.relationship.end()
        client.force_login(coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"text": "999"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        presc.refresh_from_db()
        assert presc.text == "4 x 6, RPE 7, 70"

    def test_patch_rejects_foreign_prescription(self, client):
        plan, _, _ = seed_plan()
        # A prescription that belongs to a *different* plan.
        _, _, other_presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, other_presc),
            data=json.dumps({"text": "5 x 5"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_patch_rejects_overlong_value(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"text": "x" * 2001}),  # text cap is 2000
            content_type="application/json",
        )
        assert resp.status_code == 400
        presc.refresh_from_db()
        assert presc.text == "4 x 6, RPE 7, 70"

    def test_patch_rejects_non_string_value(self, client):
        plan, _, presc = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = client.post(
            self._url(plan, presc),
            data=json.dumps({"text": 4}),  # must be a string
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
            data=json.dumps({"text": "5 x 5"}),
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


class TestNamePatch:
    """``name`` in a cell patch renames the block-shared ``ExerciseSlot``.

    Phase 2a: the one-week swap override is gone, so a rename ALWAYS edits the
    row's block-wide identity. The React client still echoes the name on every
    autosave (even a text-only edit), so an unchanged name must be a no-op —
    it records no undo action and never touches the slot.
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

    def test_rename_renames_the_block_slot(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Front Squat"})

        assert resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Front Squat"  # block-wide rename

    def test_rename_reaches_every_weeks_cell(self, client):
        # The rename lands on the slot, so a sibling week's cell (same row)
        # resolves to the new name too — no per-week identity remains.
        plan, _, cell = seed_plan()
        week2 = WeekFactory(mesocycle=cell.week.mesocycle, index=2)
        sibling = presc(exercise_slot=cell.exercise_slot, week=week2, text="4 x 6")
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Front Squat"})

        assert resp.status_code == 200
        sibling.refresh_from_db()
        assert sibling.name == "Front Squat"

    def test_unchanged_name_echo_is_a_no_op(self, client):
        # A text-only autosave echoes the name unchanged — no rename, and no
        # undo action recorded for it.
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._patch(client, plan, cell, {"name": "Box Squat"})

        assert resp.status_code == 200
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Box Squat"
        assert PlanAction.objects.filter(plan=plan).count() == 0

    def test_name_and_text_patch_together(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._patch(
            client, plan, cell, {"name": "Hack Squat", "text": "5 x 5, RPE 8"}
        )

        assert resp.status_code == 200
        cell.refresh_from_db()
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.name == "Hack Squat"
        assert cell.text == "5 x 5, RPE 8"


class TestCellLineWrite:
    """``api_cell_line_write`` — the sparse (slot × week × line) upsert (Phase 2a)."""

    def _url(self, plan, slot):
        return reverse(
            "meso:api_cell_line_write",
            kwargs={"plan_id": plan.pk, "slot_id": slot.pk},
        )

    def _post(self, client, plan, slot, body):
        return client.post(
            self._url(plan, slot),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_creates_a_sub_line_cell(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": cell.week_id, "line": 1, "text": "RPE 8"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "history" in body
        sub = Prescription.objects.get(
            exercise_slot=cell.exercise_slot, week=cell.week, line=1
        )
        assert sub.text == "RPE 8"
        assert body["cell"] == {
            "id": sub.pk,
            "exercise_slot_id": cell.exercise_slot_id,
            "week_id": cell.week_id,
            "line": 1,
            "text": "RPE 8",
        }

    def test_line_0_rewrites_the_existing_prescription_cell_in_place(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": cell.week_id, "line": 0, "text": "5 x 5"},
        )

        assert resp.status_code == 200
        assert resp.json()["cell"]["id"] == cell.pk  # the get half hit
        cell.refresh_from_db()
        assert cell.text == "5 x 5"

    def test_blank_text_clears_the_sub_line_in_place(self, client):
        # Spreadsheet semantics: clearing blanks the cell, never deletes it.
        plan, _, cell = seed_plan()
        sub = sub_line(cell, "RPE 8")
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": cell.week_id, "line": sub.line, "text": ""},
        )

        assert resp.status_code == 200
        assert resp.json()["cell"]["id"] == sub.pk  # same row, blanked
        sub.refresh_from_db()
        assert sub.text == ""

    def test_line_over_the_cap_is_400(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": cell.week_id, "line": 21, "text": "x"},
        )

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_foreign_week_is_400(self, client):
        plan, _, cell = seed_plan()
        _, _, other_cell = seed_plan()  # a week on an unrelated plan's block
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": other_cell.week_id, "line": 1, "text": "RPE 8"},
        )

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_non_owner_forbidden(self, client):
        plan, _, cell = seed_plan()
        client.force_login(UserFactory())

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"week_id": cell.week_id, "line": 1, "text": "RPE 8"},
        )

        assert resp.status_code == 403


class TestExerciseSlotPatch:
    """``api_exercise_slot_patch`` — the per-exercise Tempo/Rest/note columns (D2)."""

    def _post(self, client, plan, slot, body):
        return client.post(
            reverse(
                "meso:api_exercise_slot_patch",
                kwargs={"plan_id": plan.pk, "slot_id": slot.pk},
            ),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_writes_the_slot_columns(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._post(
            client,
            plan,
            cell.exercise_slot,
            {"tempo": "3-1-1", "rest": "120", "note": "brace hard"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "history" in body
        assert body["row"] == {
            "exercise_slot_id": cell.exercise_slot_id,
            "tempo": "3-1-1",
            "rest": "120",
            "note": "brace hard",
        }
        slot = cell.exercise_slot
        slot.refresh_from_db()
        assert (slot.tempo, slot.rest, slot.note) == ("3-1-1", "120", "brace hard")

    def test_only_touches_provided_columns(self, client):
        plan, _, cell = seed_plan()
        slot = cell.exercise_slot
        slot.tempo = "2-0-2"
        slot.save(update_fields=["tempo"])
        client.force_login(plan.relationship.coach)

        resp = self._post(client, plan, slot, {"rest": "90"})

        assert resp.status_code == 200
        slot.refresh_from_db()
        assert slot.rest == "90"
        assert slot.tempo == "2-0-2"  # untouched

    def test_rejects_overlong_value(self, client):
        plan, _, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = self._post(client, plan, cell.exercise_slot, {"tempo": "x" * 65})

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_non_owner_forbidden(self, client):
        plan, _, cell = seed_plan()
        client.force_login(UserFactory())

        resp = self._post(client, plan, cell.exercise_slot, {"rest": "90"})

        assert resp.status_code == 403
        cell.exercise_slot.refresh_from_db()
        assert cell.exercise_slot.rest == ""
