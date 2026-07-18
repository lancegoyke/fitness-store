"""P2 — Exceptions (issue #440): skip / add-this-week / fill.

Text-first (Phase 2a): the one-week ``prescription_swap`` endpoint (and the
``swap_*`` cell fields) are retired — a substitution is freeform sub-line text
now (``api_cell_line_write``, covered in ``test_designer_save.py``). What
remains here is the coach WRITE UX for the surviving exceptions:

- ``session_add_exercise`` extended with an optional ``week_id`` body key
  (add-this-week — the new row lands trained only on that week, skipped on
  every other live week of the block);
- ``prescription_skip`` — toggles a cell's one-week ``skipped`` exception;
- ``prescription_fill`` — copies a cell's whole freeform text stack (line 0 +
  sub-lines) across sibling weeks of the same block-shared row, blanking a
  target's stale higher lines in place and leaving its ``skipped`` alone.

These tests assert only OBSERVABLE state: response status/``ok``, DB fields
after ``refresh_from_db()``, and reflection through a follow-up GET to
``api_mesocycle_grid``. For ``history`` we only assert the key is present —
never its internal shape (that's ``test_designer_undo.py``'s job).
"""

import json

import pytest
from django.urls import reverse

from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import Prescription
from store_project.users.factories import UserFactory

from ._helpers import day
from ._helpers import presc
from ._helpers import sub_line

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def seed_plan(coach=None, athlete=None):
    """A minimal owned plan: one current week -> session -> prescription cell."""
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
    return plan, meso, week, session, cell


def seed_two_week_plan(coach=None, athlete=None):
    """``seed_plan`` plus a second (non-current) week reusing the same row.

    Mirrors the contract's "build week2, then ``presc(exercise_slot=row,
    week=week2, ...)`` reusing the slot" — a real multi-week block never
    duplicates the ``ExerciseSlot``.
    """
    plan, meso, week1, session, cell1 = seed_plan(coach=coach, athlete=athlete)
    week2 = WeekFactory(mesocycle=meso, index=2)
    cell2 = presc(
        exercise_slot=cell1.exercise_slot,
        week=week2,
        sets="4",
        reps="6",
        load="70",
        rpe="7",
    )
    return plan, meso, week1, week2, session, cell1, cell2


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def skip_url(plan, cell):
    return reverse(
        "meso:api_prescription_skip", kwargs={"plan_id": plan.pk, "pk": cell.pk}
    )


def fill_url(plan, cell):
    return reverse(
        "meso:api_prescription_fill", kwargs={"plan_id": plan.pk, "pk": cell.pk}
    )


def add_exercise_url(plan, session):
    return reverse(
        "meso:api_session_add_exercise",
        kwargs={"plan_id": plan.pk, "pk": session.pk},
    )


def grid_url(plan):
    return reverse("meso:api_mesocycle_grid", kwargs={"plan_id": plan.pk})


def undo_url(plan):
    return reverse("meso:api_plan_undo", kwargs={"plan_id": plan.pk})


def _grid_cell(client, plan, exercise_slot_id, week_id):
    """The grid GET's cell dict for ``exercise_slot_id`` x ``week_id``, or ``None``."""
    resp = client.get(grid_url(plan))
    assert resp.status_code == 200
    body = resp.json()
    for day_data in body["days"]:
        for row in day_data["rows"]:
            if row["exercise_slot_id"] == exercise_slot_id:
                return row["cells"].get(str(week_id))
    return None


# ---------------------------------------------------------------------------
# B1 — prescription_skip
# ---------------------------------------------------------------------------


class TestPrescriptionSkip:
    def test_skip_persists_and_reflects_in_the_grid(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(client, skip_url(plan, cell), {"skipped": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "history" in body
        cell.refresh_from_db()
        assert cell.skipped is True
        grid_cell = _grid_cell(client, plan, cell.exercise_slot_id, week.pk)
        assert grid_cell["skipped"] is True

    def test_unskip_sets_skipped_false(self, client):
        plan, meso, week, session, cell = seed_plan()
        cell.skipped = True
        cell.save(update_fields=["skipped"])
        client.force_login(plan.relationship.coach)

        resp = post_json(client, skip_url(plan, cell), {"skipped": False})

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        cell.refresh_from_db()
        assert cell.skipped is False
        grid_cell = _grid_cell(client, plan, cell.exercise_slot_id, week.pk)
        assert grid_cell["skipped"] is False

    def test_skip_then_undo_restores_skipped_false(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)
        resp = post_json(client, skip_url(plan, cell), {"skipped": True})
        assert resp.status_code == 200
        cell.refresh_from_db()
        assert cell.skipped is True  # sanity: the skip landed

        resp = client.post(undo_url(plan))

        assert resp.status_code == 200
        cell.refresh_from_db()
        assert cell.skipped is False

    def test_missing_skipped_key_is_400(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(client, skip_url(plan, cell), {})

        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        cell.refresh_from_db()
        assert cell.skipped is False

    def test_non_bool_skipped_is_400(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(client, skip_url(plan, cell), {"skipped": "true"})

        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        cell.refresh_from_db()
        assert cell.skipped is False

    def test_get_not_allowed(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.get(skip_url(plan, cell))

        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# B0 — session_add_exercise extended with week_id (add-this-week)
# ---------------------------------------------------------------------------


class TestAddExerciseThisWeek:
    def test_week_id_trains_only_that_week_and_skips_the_others(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(client, add_exercise_url(plan, session), {"week_id": week2.pk})

        assert resp.status_code == 201
        body = resp.json()
        assert body["ok"] is True
        assert "history" in body
        target_cell = Prescription.objects.get(pk=body["prescription"]["id"])
        assert target_cell.week_id == week2.pk
        assert target_cell.skipped is False

        sibling = Prescription.objects.get(
            exercise_slot_id=target_cell.exercise_slot_id, week=week1
        )
        assert sibling.skipped is True

        # Reflected through the grid too.
        grid_target = _grid_cell(client, plan, target_cell.exercise_slot_id, week2.pk)
        assert grid_target["skipped"] is False
        grid_sibling = _grid_cell(client, plan, target_cell.exercise_slot_id, week1.pk)
        assert grid_sibling["skipped"] is True

    def test_missing_week_id_trains_all_weeks_regression(self, client):
        # Byte-identical unscoped behavior — this should already pass.
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        client.force_login(plan.relationship.coach)

        resp = client.post(add_exercise_url(plan, session))  # no body at all

        assert resp.status_code == 201
        target_cell = Prescription.objects.get(pk=resp.json()["prescription"]["id"])
        assert target_cell.skipped is False
        sibling = Prescription.objects.get(
            exercise_slot_id=target_cell.exercise_slot_id, week=week2
        )
        assert sibling.skipped is False

    def test_nonexistent_week_id_is_400(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(client, add_exercise_url(plan, session), {"week_id": 999999})

        assert resp.status_code == 400
        assert resp.json()["ok"] is False

    def test_foreign_week_id_is_400(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        _, _, other_week, _, _ = seed_plan()  # a week from an unrelated plan
        client.force_login(plan.relationship.coach)

        resp = post_json(
            client, add_exercise_url(plan, session), {"week_id": other_week.pk}
        )

        assert resp.status_code == 400
        assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# B3 — prescription_fill
# ---------------------------------------------------------------------------


class TestPrescriptionFill:
    def test_fill_copies_the_text_stack_to_the_other_week_by_default(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        cell1.text = "5 x 3, RPE 9, 80"
        cell1.save(update_fields=["text"])
        sub = sub_line(cell1, "belt on")  # a sub-line rides the stack too
        client.force_login(plan.relationship.coach)

        resp = client.post(fill_url(plan, cell1))  # no body -> defaults to all others

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["filled"] == 1
        assert "history" in body
        cell2.refresh_from_db()
        assert cell2.text == "5 x 3, RPE 9, 80"
        copied_sub = Prescription.objects.get(
            exercise_slot_id=cell1.exercise_slot_id, week=week2, line=sub.line
        )
        assert copied_sub.text == "belt on"
        grid_cell = _grid_cell(client, plan, cell1.exercise_slot_id, week2.pk)
        assert grid_cell["text"] == "5 x 3, RPE 9, 80"
        assert [line["text"] for line in grid_cell["lines"]] == ["belt on"]

    def test_fill_blanks_a_targets_stale_higher_lines_in_place(self, client):
        # The target week carries a sub-line the source lacks — fill blanks it
        # (spreadsheet semantics), never deletes the row.
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        stale = sub_line(cell2, "old cue")
        client.force_login(plan.relationship.coach)

        resp = client.post(fill_url(plan, cell1))

        assert resp.status_code == 200
        stale.refresh_from_db()  # same pk — blanked, not deleted
        assert stale.text == ""

    def test_fill_leaves_a_siblings_skipped_alone(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        cell2.skipped = True
        cell2.save(update_fields=["skipped"])
        cell1.text = "4 x 6, RPE 7, 85"
        cell1.save(update_fields=["text"])
        client.force_login(plan.relationship.coach)

        resp = client.post(fill_url(plan, cell1))

        assert resp.status_code == 200
        cell2.refresh_from_db()
        assert cell2.text == "4 x 6, RPE 7, 85"  # text still copied
        assert cell2.skipped is True  # the one-week exception untouched

    def test_fill_with_explicit_week_ids_targets_only_those_weeks(self, client):
        plan, meso, week1, week2, session, cell1, cell2 = seed_two_week_plan()
        week3 = WeekFactory(mesocycle=meso, index=3)
        cell3 = presc(
            exercise_slot=cell1.exercise_slot,
            week=week3,
            text="3 x 10, RPE 7, 60",
        )
        cell1.text = "4 x 6, RPE 7, 88"
        cell1.save(update_fields=["text"])
        client.force_login(plan.relationship.coach)

        resp = post_json(client, fill_url(plan, cell1), {"week_ids": [week3.pk]})

        assert resp.status_code == 200
        assert resp.json()["filled"] == 1
        cell3.refresh_from_db()
        assert cell3.text == "4 x 6, RPE 7, 88"
        cell2.refresh_from_db()
        assert cell2.text == "4 x 6, RPE 7, 70"  # untouched — not a targeted week

    def test_get_not_allowed(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = client.get(fill_url(plan, cell))

        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Ownership + billing — one representative per gate (shared by B1-B3)
# ---------------------------------------------------------------------------


class TestOwnershipAndBilling:
    def test_non_owner_cannot_skip(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(UserFactory())  # a stranger

        resp = post_json(client, skip_url(plan, cell), {"skipped": True})

        assert resp.status_code == 403
        cell.refresh_from_db()
        assert cell.skipped is False

    def test_missing_plan_is_404(self, client):
        plan, meso, week, session, cell = seed_plan()
        client.force_login(plan.relationship.coach)

        resp = post_json(
            client,
            reverse(
                "meso:api_prescription_skip",
                kwargs={"plan_id": 999999, "pk": cell.pk},
            ),
            {"skipped": True},
        )

        assert resp.status_code == 404

    def test_foreign_cell_is_404(self, client):
        plan, meso, week, session, cell = seed_plan()
        _, _, _, _, other_cell = seed_plan()  # belongs to a different plan
        client.force_login(plan.relationship.coach)

        resp = post_json(client, skip_url(plan, other_cell), {"skipped": True})

        assert resp.status_code == 404

    def test_over_limit_coach_gets_402(self, client):
        # Mirrors test_billing_enforcement: an older kept link plus this plan's
        # newer one pushes the free coach over the cap, freezing this plan.
        coach = UserFactory()
        CoachAthleteFactory(coach=coach, status=CoachAthlete.Status.ACTIVE)  # kept
        plan, meso, week, session, cell = seed_plan(coach=coach)  # newer -> suspended
        client.force_login(coach)

        resp = post_json(client, skip_url(plan, cell), {"skipped": True})

        assert resp.status_code == 402
        assert resp.json()["ok"] is False
        cell.refresh_from_db()
        assert cell.skipped is False
