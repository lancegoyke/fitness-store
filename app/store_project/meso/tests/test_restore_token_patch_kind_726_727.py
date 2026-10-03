"""#726 — a restore keeps the replay token on a line it leaves unchanged.

#727 — ``prescription_patch`` refuses coach text on a blank athlete sub-line.
"""

import pytest
from django.urls import reverse

from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.tests.test_coach_logs_709 import all_sets
from store_project.meso.tests.test_coach_logs_709 import cells
from store_project.meso.tests.test_coach_logs_709 import patch_text
from store_project.meso.tests.test_coach_logs_709 import redo
from store_project.meso.tests.test_coach_logs_709 import skip_row
from store_project.meso.tests.test_coach_logs_709 import start
from store_project.meso.tests.test_coach_logs_709 import undo
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def _tokened_set(client, s):
    """Session started, a cue on line 1, then a coach set that relocated to 2."""
    start(s)
    client.force_login(s.coach)
    assert coach_write(client, s, "brace").status_code == 200
    first = coach_write(client, s, "225 x 5", intent="new", kind="set", token="t1")
    assert first.status_code == 200 and first.json()["relocated_from"] == 1
    assert cells(s)[2].client_token == "t1"
    return first


class TestRestoreKeepsTokenOnAnUnchangedLine:
    def test_replay_after_skip_and_undo_lands_once(self, client):
        s = seed()
        _tokened_set(client, s)
        skip_row(client, s, True)
        undo(client, s)
        n_actions = PlanAction.objects.filter(plan=s.plan).count()
        again = coach_write(client, s, "225 x 5", intent="new", kind="set", token="t1")
        assert again.status_code == 200
        assert again.json()["relocated_from"] == 1
        assert [c.text for c in cells(s).values()].count("225 x 5") == 1
        assert sorted(cells(s)) == [1, 2]
        assert len(all_sets(s)) == 1
        assert PlanAction.objects.filter(plan=s.plan).count() == n_actions

    def test_token_survives_an_unrelated_undo(self, client):
        s = seed()
        _tokened_set(client, s)
        skip_row(client, s, True)
        undo(client, s)
        assert sub_cell(s.squat, 2).client_token == "t1"

    def test_undoing_the_write_itself_clears_the_token(self, client):
        # The undo changes the line (it goes away or reverts), so no cell may
        # still answer to the token. (The #709 guard
        # test_a_replay_after_undo_does_not_match_the_restored_line covers the
        # replay side.)
        s = seed()
        _tokened_set(client, s)
        undo(client, s)
        assert not Prescription.objects.filter(
            exercise_slot=s.squat.exercise_slot, client_token="t1"
        ).exists()
        assert LoggedSet.objects.filter(session_log__session=s.session).count() == 0


def _replay_lands_once(client, s):
    n_actions = PlanAction.objects.filter(plan=s.plan).count()
    again = coach_write(client, s, "225 x 5", intent="new", kind="set", token="t1")
    assert again.status_code == 200
    assert again.json()["relocated_from"] == 1
    assert [c.text for c in cells(s).values()].count("225 x 5") == 1
    assert len(all_sets(s)) == 1
    assert PlanAction.objects.filter(plan=s.plan).count() == n_actions


class TestRestoreReturnsTheRecordedToken:
    def test_replay_after_undo_and_redo_of_the_write_lands_once(self, client):
        s = seed()
        _tokened_set(client, s)
        undo(client, s)
        redo(client, s)
        _replay_lands_once(client, s)

    def test_replay_after_undoing_a_same_text_chip_flip_lands_once(self, client):
        s = seed()
        _tokened_set(client, s)
        flip = coach_write(client, s, "225 x 5", line=2, kind="cue")
        assert flip.status_code == 200, flip.content
        assert cells(s)[2].client_token == "t1"
        undo(client, s)
        assert cells(s)[2].is_coach_set
        _replay_lands_once(client, s)


class TestRestorePutsBackExactlyTheRecordedToken:
    def test_undoing_a_same_text_write_leaves_no_token_to_replay(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        assert coach_write(client, s, "225 x 5", kind="set").status_code == 200
        assert cells(s)[1].client_token == ""
        resp = coach_write(client, s, "225 x 5", intent="new", kind="set", token="t3")
        assert resp.status_code == 200, resp.content
        assert cells(s)[1].client_token == "t3"
        undo(client, s)
        assert cells(s)[1].client_token == ""

    def test_one_token_never_ends_up_on_two_cells(self, client):
        s = seed()
        client.force_login(s.coach)
        assert coach_write(client, s, "Z").status_code == 200
        w = coach_write(client, s, "X", intent="new", token="T")
        assert w.status_code == 200 and w.json()["cell"]["line"] == 2
        assert coach_write(client, s, "X", line=1).status_code == 200
        assert coach_write(client, s, "Y", line=2).status_code == 200
        again = coach_write(client, s, "X", intent="new", token="T")
        assert again.status_code == 200, again.content
        undo(client, s)
        undo(client, s)
        holders = Prescription.objects.filter(client_token="T")
        assert holders.count() <= 1
        replay = coach_write(client, s, "X", intent="new", token="T")
        assert replay.status_code == 200
        if holders.count():
            assert replay.json()["cell"]["id"] == holders.get().pk


def _blank_athlete_line(client, s):
    client.force_login(s.athlete)
    write_cell(client, s.session, s.squat, 1, "225 x 5")
    write_cell(client, s.session, s.squat, 1, "")
    cell = sub_cell(s.squat, 1)
    assert cell.text == "" and cell.athlete_authored
    return cell


def _patch_url(s, cell):
    return reverse(
        "meso:api_prescription_patch", kwargs={"plan_id": s.plan.pk, "pk": cell.pk}
    )


class TestPatchRefusesABlankAthleteLine:
    def test_coach_text_on_a_blank_athlete_line_is_422(self, client):
        s = seed()
        cell = _blank_athlete_line(client, s)
        client.force_login(s.coach)
        n_actions = PlanAction.objects.filter(plan=s.plan).count()
        resp = patch_text(client, s, cell, "brace")
        assert resp.status_code == 422
        assert resp.json()["code"] == "blank_athlete_line"
        cell.refresh_from_db()
        assert cell.text == "" and cell.athlete_authored
        assert not cell.entered_by_coach
        assert PlanAction.objects.filter(plan=s.plan).count() == n_actions


class TestPatchStillEditsCuesAndLineZero:
    def test_cue_sub_line_text_patch_saves(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "brace")
        cell = sub_cell(s.squat, 1)
        resp = patch_text(client, s, cell, "brace harder")
        assert resp.status_code == 200, resp.content
        cell.refresh_from_db()
        assert cell.text == "brace harder"

    def test_line_zero_text_patch_saves(self, client):
        s = seed()
        client.force_login(s.coach)
        resp = patch_text(client, s, s.squat, "3x5")
        assert resp.status_code == 200, resp.content
        s.squat.refresh_from_db()
        assert s.squat.text == "3x5"
