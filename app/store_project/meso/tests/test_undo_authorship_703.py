"""#703 — a coach undo of a rewrite hands the athlete's own line back.

The athlete types ``225 x 5``; the coach rewrites that line (which reclaims it
into coach history); the coach undoes. The cell must come back as the
athlete's (``athlete_authored=True``), not as a coach cue the athlete can no
longer correct (#524 guard, 422). Redo takes the line again only if the athlete
hasn't touched it since (compare-and-set).
"""

import json

import pytest
from django.urls import reverse

from store_project.meso import presenters
from store_project.meso.history import record_plan_action
from store_project.meso.history import restore_plan_snapshot
from store_project.meso.history import serialize_plan_snapshot
from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.tests.test_parse_at_commit import legacy_reclaim
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def _post(client, name, s, **kw):
    url = reverse(f"meso:{name}", kwargs={"plan_id": s.plan.pk})
    resp = client.post(url, content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp


def undo(client, s):
    return _post(client, "api_plan_undo", s)


def redo(client, s):
    return _post(client, "api_plan_redo", s)


def view(s):
    ctx = presenters.athlete_session(s.session, s.athlete)
    return next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)


def texts(rows):
    return [r["text"] for r in rows]


def typed_then_rewritten(client, s, coach_text="brace harder"):
    client.force_login(s.athlete)
    assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200
    client.force_login(s.coach)
    legacy_reclaim(s, text=coach_text)
    cell = sub_cell(s.squat, 1)
    assert cell.athlete_authored is False and cell.text == coach_text
    return cell


def assert_athlete_line(s, text="225 x 5"):
    cell = sub_cell(s.squat, 1)
    assert cell.athlete_authored is True
    assert cell.text == text
    v = view(s)
    assert text in texts(v["sub_lines"])
    assert text not in texts(v["coach_lines"])


def assert_coach_line(s, text):
    cell = sub_cell(s.squat, 1)
    assert cell.athlete_authored is False
    assert cell.text == text
    v = view(s)
    assert text in texts(v["coach_lines"])
    assert texts(v["sub_lines"]) == []


class TestUndoHandsTheLineBack:
    def test_undo_restores_athlete_authorship_and_correction_works(self, client):
        s = seed()
        typed_then_rewritten(client, s)
        undo(client, s)
        assert_athlete_line(s)

        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 6")
        assert resp.status_code == 200
        rows = list(LoggedSet.objects.filter(session_log__session=s.session))
        assert len(rows) == 1
        assert str(rows[0].reps) == "6"

    def test_redo_then_undo_round_trips_twice(self, client):
        s = seed()
        typed_then_rewritten(client, s)
        for _ in range(2):
            undo(client, s)
            assert_athlete_line(s)
            redo(client, s)
            assert_coach_line(s, "brace harder")
        undo(client, s)
        assert_athlete_line(s)
        assert LoggedSet.objects.filter(session_log__session=s.session).count() == 1

    def test_athlete_edit_after_undo_survives_redo(self, client):
        s = seed()
        typed_then_rewritten(client, s)
        undo(client, s)
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 1, "225 x 6").status_code == 200

        client.force_login(s.coach)
        redo(client, s)
        assert_athlete_line(s, "225 x 6")
        # and undoing the redo must not revert the athlete's line either
        undo(client, s)
        assert_athlete_line(s, "225 x 6")

    def test_clear_variant(self, client):
        s = seed()
        typed_then_rewritten(client, s, coach_text="")
        undo(client, s)
        assert_athlete_line(s)
        redo(client, s)
        cell = sub_cell(s.squat, 1)
        assert cell.athlete_authored is False
        assert cell.text == ""

    def test_undo_writes_no_athlete_data(self, client):
        s = seed()
        typed_then_rewritten(client, s)
        fields = ("pk", "source_line_id", "set_number", "reps", "load")
        before = sorted(LoggedSet.objects.values_list(*fields))
        undo(client, s)
        redo(client, s)
        undo(client, s)
        assert sorted(LoggedSet.objects.values_list(*fields)) == before


class TestFillLeavesAthleteLineAlone:
    def test_fill_undo_redo(self, client):
        from store_project.meso.factories import WeekFactory

        s = seed()
        week1 = WeekFactory(mesocycle=s.meso, index=1)
        slot = s.squat.exercise_slot
        src0 = Prescription.objects.create(exercise_slot=slot, week=week1, line=0)
        Prescription.objects.create(
            exercise_slot=slot, week=week1, line=1, text="coach cue"
        )
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 1, "225 x 5").status_code == 200

        client.force_login(s.coach)
        url = reverse(
            "meso:api_prescription_fill",
            kwargs={"plan_id": s.plan.pk, "pk": src0.pk},
        )
        resp = client.post(url, data=json.dumps({}), content_type="application/json")
        assert resp.status_code == 200
        assert_athlete_line(s)
        undo(client, s)
        assert_athlete_line(s)
        redo(client, s)
        assert_athlete_line(s)


class TestBackCompat:
    def _snap_row(self, cell, text):
        return {
            "pk": cell.pk,
            "exercise_slot_id": cell.exercise_slot_id,
            "week_id": cell.week_id,
            "line": cell.line,
            "text": text,
            "skipped": False,
        }

    def test_old_snapshot_writes_coach_cell_over_coach_cell(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        Prescription.objects.filter(pk=cell.pk).update(athlete_authored=False)
        snap = serialize_plan_snapshot(s.plan)
        snap["cells"] = [r for r in snap["cells"] if r["pk"] != cell.pk] + [
            self._snap_row(cell, "old text")
        ]
        restore_plan_snapshot(s.plan, snap)
        cell.refresh_from_db()
        assert cell.text == "old text"
        assert cell.athlete_authored is False

    def test_unmarked_coach_row_never_overwrites_athlete_cell(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        snap = serialize_plan_snapshot(s.plan)
        snap["cells"].append(self._snap_row(cell, "old text"))
        restore_plan_snapshot(s.plan, snap)
        cell.refresh_from_db()
        assert cell.text == "225 x 5"
        assert cell.athlete_authored is True


class TestSnapshotShape:
    def test_plain_serialize_excludes_athlete_cells(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        snap = serialize_plan_snapshot(s.plan)
        assert cell.pk not in [r["pk"] for r in snap["cells"]]

    def test_record_plan_action_captures_named_athlete_cell(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        record_plan_action(s.plan, "x", athlete_cell_pks=[cell.pk])
        snap = PlanAction.objects.get(plan=s.plan).snapshot
        row = next(r for r in snap["cells"] if r["pk"] == cell.pk)
        assert row["athlete_authored"] is True
        assert row["text"] == "225 x 5"
        assert all(
            "athlete_authored" not in r for r in snap["cells"] if r["pk"] != cell.pk
        )
