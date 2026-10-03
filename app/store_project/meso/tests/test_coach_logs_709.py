"""#709 — the coach logs the athlete's sets from the designer grid.

A sub-line is a cue, an athlete set line, or a coach set line
(``Prescription.athlete_authored`` + ``entered_by_coach``). The coach's write
endpoint never overwrites an athlete's line, a write that collides lands on the
next free line, and a coach set derives its ``LoggedSet`` through the same
writer the athlete's blur uses.
"""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import one_rm
from store_project.meso import personal_records
from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def start(s):
    """The athlete has opened the session: a SessionLog exists, with no sets."""
    return SessionLog.objects.create_for_pair(
        s.session, s.athlete, date=timezone.localdate()
    )


def sets_of(s, line=1):
    return list(LoggedSet.objects.filter(source_line=sub_cell(s.squat, line)))


def all_sets(s):
    return list(LoggedSet.objects.filter(session_log__session=s.session))


def cells(s):
    return {
        c.line: c
        for c in Prescription.objects.filter(
            exercise_slot=s.squat.exercise_slot, week=s.week, line__gte=1
        )
    }


def undo_redo(client, s, name):
    resp = client.post(
        reverse(f"meso:{name}", kwargs={"plan_id": s.plan.pk}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    return resp


def undo(client, s):
    return undo_redo(client, s, "api_plan_undo")


def redo(client, s):
    return undo_redo(client, s, "api_plan_redo")


def page_row(s):
    ctx = presenters.athlete_session(s.session, s.athlete)
    return next(e for e in ctx["exercises"] if e["id"] == s.squat.pk)


@pytest.fixture
def coach(client):
    def _login(s):
        client.force_login(s.coach)
        return client

    return _login


class TestCoachSetOnAStartedSession:
    @pytest.mark.parametrize("text", ["225x5", "5 @ 225"])
    def test_is_stored_as_a_coach_set_line_and_counts(self, client, text):
        s = seed()
        log = start(s)
        client.force_login(s.coach)
        resp = coach_write(client, s, text)
        assert resp.status_code == 200, resp.content
        # The performance first: on main a coach line derives no set at all.
        rows = sets_of(s)
        assert len(rows) == 1
        assert rows[0].session_log_id == log.pk
        assert (rows[0].load, rows[0].reps) == ("225", "5")
        cell = sub_cell(s.squat, 1)
        assert cell.is_coach_set
        body = resp.json()
        assert body["cell"]["athlete_authored"] is True
        assert body["cell"]["entered_by_coach"] is True
        assert "relocated_from" not in body

        # counts in the athlete's "N of M" ...
        assert presenters.athlete_set_progress(s.session, s.athlete)["logged"] == 1
        # ... in the live PRs ...
        records = personal_records.personal_records(s.athlete, unit=s.plan.unit)
        assert len(records) == 1
        # ... and the 1RM once the log is finished (1RM is DONE-only)
        log.status = SessionLog.Status.DONE
        log.save(update_fields=["status"])
        assert len(one_rm.derive_one_rm_values(s.athlete, unit=s.plan.unit)) == 1
        results = presenters.session_results(s.session)
        assert results["summary"]["logged_sets"] == 1

        # and shows on the athlete's page as the coach's line
        entry = next(x for x in page_row(s)["sub_lines"] if x["line"] == 1)
        assert entry["text"] == text
        assert entry["entered_by_coach"] is True

    def test_the_athletes_own_set_event_is_not_fired(self, client):
        # Control: the athlete's own typed set DOES fire it (so the absence
        # below is the coach path's doing, not a muted tracker).
        c = seed()
        client.force_login(c.athlete)
        write_cell(client, c.session, c.squat, 1, "225 x 5")
        assert Event.objects.filter(name=EventName.SET_LOGGED).exists()
        Event.objects.all().delete()
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        assert not Event.objects.filter(
            name=EventName.SET_LOGGED, actor=s.athlete
        ).exists()

    def test_the_same_text_before_the_session_starts_is_a_cue(self, client):
        s = seed()
        client.force_login(s.coach)
        resp = coach_write(client, s, "225x5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["entered_by_coach"] is False
        assert resp.json()["cell"]["athlete_authored"] is False
        assert not all_sets(s)
        assert not SessionLog.objects.filter(session=s.session).exists()

    def test_sets_by_reps_is_a_cue_by_default(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        resp = coach_write(client, s, "3x5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["entered_by_coach"] is False
        assert not sub_cell(s.squat, 1).athlete_authored
        assert not all_sets(s)

    def test_last_activity_is_bumped_and_status_untouched(self, client):
        s = seed()
        log = start(s)
        old = timezone.now() - timezone.timedelta(days=2)
        SessionLog.objects.filter(pk=log.pk).update(last_activity_at=old)
        client.force_login(s.coach)
        assert coach_write(client, s, "225x5").status_code == 200
        log.refresh_from_db()
        assert log.last_activity_at > old
        assert log.status == SessionLog.Status.PENDING


class TestChipFlip:
    def test_kind_set_on_a_cue_creates_the_set_and_the_log(self, client):
        s = seed()
        client.force_login(s.coach)
        assert coach_write(client, s, "225x5").status_code == 200  # a cue: not started
        assert not SessionLog.objects.filter(session=s.session).exists()

        resp = coach_write(client, s, "225x5", kind="set")
        assert resp.status_code == 200, resp.content
        assert resp.json()["cell"]["entered_by_coach"] is True
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert log.status == SessionLog.Status.PENDING
        assert len(sets_of(s)) == 1
        assert not Event.objects.filter(
            name=EventName.SET_LOGGED, actor=s.athlete
        ).exists()

    def test_kind_cue_on_a_coach_set_removes_the_derived_set(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        assert len(sets_of(s)) == 1
        resp = coach_write(client, s, "225x5", kind="cue")
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        assert not cell.athlete_authored and not cell.entered_by_coach
        assert not all_sets(s)

    def test_kind_set_on_text_that_is_not_a_set_is_refused(self, client):
        s = seed()
        client.force_login(s.coach)
        resp = coach_write(client, s, "brace harder", kind="set")
        assert resp.status_code == 422
        assert resp.json()["code"] == "not_a_set"
        assert not cells(s)
        assert not SessionLog.objects.filter(session=s.session).exists()
        assert not PlanAction.objects.filter(plan=s.plan).exists()

    def test_kind_set_on_a_template_is_refused(self, client):
        s = seed()
        # A plan with no athlete: the relationship carries the athlete, so use a
        # real template via the factory.
        from store_project.meso.factories import PlanFactory

        template = PlanFactory(
            relationship=None, owner=s.coach, is_template=True, title="Tpl"
        )
        from store_project.meso.factories import MesocycleFactory
        from store_project.meso.factories import WeekFactory
        from store_project.meso.tests._helpers import day
        from store_project.meso.tests._helpers import presc

        meso = MesocycleFactory(plan=template, name="T", order=0)
        week = WeekFactory(mesocycle=meso, index=1)
        sess = day(week, day_number=1, name="D")
        squat = presc(
            sess, name="Squat", order=0, sets="3", reps="5", load="70", rpe="7"
        )
        client.force_login(s.coach)
        resp = client.post(
            reverse(
                "meso:api_cell_line_write",
                kwargs={"plan_id": template.pk, "slot_id": squat.exercise_slot.pk},
            ),
            data=json.dumps(
                {"week_id": week.pk, "line": 1, "text": "225x5", "kind": "set"}
            ),
            content_type="application/json",
        )
        assert resp.status_code == 422, resp.content
        assert resp.json()["code"] == "no_athlete"
        assert not Prescription.objects.filter(
            exercise_slot=squat.exercise_slot, week=week, line=1
        ).exists()


class TestBadBodies:
    @pytest.mark.parametrize(
        "extra,line",
        [
            ({"intent": "bogus"}, 1),
            ({"kind": "bogus"}, 1),
            ({"kind": "set"}, 0),
            ({"kind": "cue"}, 0),
        ],
    )
    def test_is_a_400(self, client, extra, line):
        s = seed()
        client.force_login(s.coach)
        resp = coach_write(client, s, "225x5", line=line, **extra)
        assert resp.status_code == 400
        assert not cells(s)


class TestUndoRedo:
    def test_undo_removes_the_line_and_its_set_and_reaps_the_log(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        assert len(sets_of(s)) == 1

        undo(client, s)
        assert not all_sets(s)
        assert not cells(s) or not sub_cell(s.squat, 1).text
        # the athlete started this log (``start``), so it is theirs even empty:
        # a coach undo reaps only a log its own set opened (#719.1; the reap
        # itself is covered in test_coach_set_undo_skip_717_719.py)
        assert SessionLog.objects.filter(session=s.session).exists()

        redo(client, s)
        cell = sub_cell(s.squat, 1)
        assert (cell.text, cell.is_coach_set) == ("225x5", True)
        assert len(sets_of(s)) == 1
        assert SessionLog.objects.filter(session=s.session).exists()

    def test_undo_reaps_a_log_the_set_created(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "225x5")  # a cue
        coach_write(client, s, "225x5", kind="set")  # creates the log
        assert SessionLog.objects.filter(session=s.session).exists()
        undo(client, s)
        assert not all_sets(s)
        assert not SessionLog.objects.filter(session=s.session).exists()
        assert not sub_cell(s.squat, 1).athlete_authored

    def test_undo_of_a_chip_flip_restores_the_previous_kind_and_sets(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        coach_write(client, s, "225x5", kind="cue")
        assert not all_sets(s)

        undo(client, s)  # back to a coach set line
        cell = sub_cell(s.squat, 1)
        assert cell.is_coach_set
        assert len(sets_of(s)) == 1

        redo(client, s)
        assert not sub_cell(s.squat, 1).athlete_authored
        assert not all_sets(s)

    def test_undo_after_the_athlete_edited_the_line_leaves_it_alone(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 1, "230 x 3").status_code == 200

        client.force_login(s.coach)
        undo(client, s)
        cell = sub_cell(s.squat, 1)
        assert (cell.text, cell.athlete_authored, cell.entered_by_coach) == (
            "230 x 3",
            True,
            False,
        )
        rows = sets_of(s)
        assert [(r.load, r.reps) for r in rows] == [("230", "3")]


class TestAthleteLineIsNeverOverwritten:
    def test_a_changed_write_is_refused_and_changes_nothing(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        before = sets_of(s)
        client.force_login(s.coach)

        resp = coach_write(client, s, "315 x 1")
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "athlete_line"
        assert "athlete_first_name" in body and body["athlete_first_name"]
        assert body["grid_cell"]["lines"][0]["text"] == "225 x 5"

        cell = sub_cell(s.squat, 1)
        assert (cell.text, cell.athlete_authored, cell.entered_by_coach) == (
            "225 x 5",
            True,
            False,
        )
        assert [r.pk for r in sets_of(s)] == [r.pk for r in before]
        assert not PlanAction.objects.filter(plan=s.plan).exists()

    def test_an_unchanged_write_is_a_200_and_records_nothing(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        resp = coach_write(client, s, "225 x 5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["athlete_authored"] is True
        assert not PlanAction.objects.filter(plan=s.plan).exists()
        assert sub_cell(s.squat, 1).athlete_authored is True


class TestAthleteEditsACoachSet:
    def test_an_edit_makes_it_theirs(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225 x 4")
        assert resp.status_code == 200
        assert resp.json()["cell"]["entered_by_coach"] is False
        cell = sub_cell(s.squat, 1)
        assert (cell.athlete_authored, cell.entered_by_coach) == (True, False)
        assert [(r.load, r.reps) for r in sets_of(s)] == [("225", "4")]

    def test_an_unchanged_blur_keeps_it_the_coachs(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        client.force_login(s.athlete)
        resp = write_cell(client, s.session, s.squat, 1, "225x5")
        assert resp.status_code == 200
        assert resp.json()["cell"]["entered_by_coach"] is True
        assert sub_cell(s.squat, 1).is_coach_set
        assert len(sets_of(s)) == 1


class TestCollisions:
    def test_coach_second_lands_on_the_next_line(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        resp = coach_write(client, s, "brace harder", intent="new")
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["relocated_from"] == 1
        assert body["cell"]["line"] == 2
        by_line = cells(s)
        assert by_line[1].text == "225 x 5" and by_line[1].athlete_authored
        assert by_line[2].text == "brace harder"
        assert [x["text"] for x in body["grid_cell"]["lines"]] == [
            "225 x 5",
            "brace harder",
        ]

    def test_athlete_second_lands_on_the_next_line_and_the_coach_set_counts(
        self, client
    ):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "100x5", line=2)
        assert sub_cell(s.squat, 2).is_coach_set
        client.force_login(s.athlete)
        resp = cell_new(client, s, 2, "225 x 5", new=True)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body["relocated_from"] == 2
        assert body["cell"]["line"] == 3
        assert {x["line"] for x in body["exercise_lines"]["sub_lines"]} == {2, 3}
        by_line = cells(s)
        assert by_line[2].is_coach_set and by_line[2].text == "100x5"
        assert by_line[3].text == "225 x 5" and not by_line[3].entered_by_coach
        assert len(all_sets(s)) == 2
        assert presenters.athlete_set_progress(s.session, s.athlete)["logged"] == 2


def cell_new(client, s, line, text, **extra):
    return client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": s.session.pk}),
        data=json.dumps(
            {"exercise_id": s.squat.pk, "line": line, "text": text, **extra}
        ),
        content_type="application/json",
    )


class TestOldAthleteClients:
    def test_without_new_a_coach_cue_still_422s(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "brace harder")
        client.force_login(s.athlete)
        resp = cell_new(client, s, 1, "225 x 5")
        assert resp.status_code == 422
        assert resp.json()["code"] == "coach_line"
        assert "exercise_lines" in resp.json()
        assert sub_cell(s.squat, 1).text == "brace harder"

    def test_without_new_a_coach_set_line_is_an_edit(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "100x5")
        client.force_login(s.athlete)
        resp = cell_new(client, s, 1, "225 x 5")
        assert resp.status_code == 200
        cell = sub_cell(s.squat, 1)
        assert (cell.text, cell.athlete_authored, cell.entered_by_coach) == (
            "225 x 5",
            True,
            False,
        )

    def test_new_must_be_a_bool(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert cell_new(client, s, 1, "x", new="yes").status_code == 400


# -- review round 1 ------------------------------------------------------------


def patch_text(client, s, cell, text):
    return client.post(
        reverse(
            "meso:api_prescription_patch", kwargs={"plan_id": s.plan.pk, "pk": cell.pk}
        ),
        data=json.dumps({"text": text}),
        content_type="application/json",
    )


def skip_row(client, s, skipped):
    resp = client.post(
        reverse(
            "meso:api_prescription_skip",
            kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
        ),
        data=json.dumps({"skipped": skipped}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content


class TestPatchHonoursRuleB:
    def test_a_changed_patch_of_an_athlete_line_is_refused(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        client.force_login(s.coach)
        resp = patch_text(client, s, cell, "315 x 1")
        assert resp.status_code == 422
        assert resp.json()["code"] == "athlete_line"
        cell.refresh_from_db()
        assert (cell.text, cell.athlete_authored) == ("225 x 5", True)
        assert len(sets_of(s)) == 1
        assert not PlanAction.objects.filter(plan=s.plan).exists()

    def test_an_unchanged_patch_is_a_200_no_op(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        cell = sub_cell(s.squat, 1)
        client.force_login(s.coach)
        resp = patch_text(client, s, cell, "225 x 5")
        assert resp.status_code == 200
        assert not PlanAction.objects.filter(plan=s.plan).exists()
        assert sub_cell(s.squat, 1).athlete_authored is True


class TestCoachSetOnASkippedRow:
    def test_blanking_the_line_while_skipped_removes_the_set(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225 x 5")
        assert len(sets_of(s)) == 1
        skip_row(client, s, True)
        assert coach_write(client, s, "").status_code == 200
        skip_row(client, s, False)
        assert not all_sets(s)
        assert presenters.athlete_set_progress(s.session, s.athlete)["logged"] == 0

    def test_a_cue_flip_undo_redo_while_skipped_keeps_the_cue_setless(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225 x 5")
        skip_row(client, s, True)
        coach_write(client, s, "225 x 5", kind="cue")
        assert not all_sets(s)
        undo(client, s)  # back to a coach set line (row still skipped)
        redo(client, s)  # and to a cue again
        skip_row(client, s, False)
        assert not sub_cell(s.squat, 1).athlete_authored
        assert not all_sets(s)

    def test_a_stale_athlete_blur_on_a_skipped_row_still_keeps_the_set(self, client):
        s = seed()
        start(s)
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        skip_row(client, s, True)
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        assert len(sets_of(s)) == 1


class TestReplayIdempotency:
    def test_athlete_replay_lands_once(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "brace harder")  # a cue on line 1
        client.force_login(s.athlete)
        first = cell_new(client, s, 1, "225 x 5", new=True, token="t-1")
        assert first.status_code == 200 and first.json()["relocated_from"] == 1
        again = cell_new(client, s, 1, "225 x 5", new=True, token="t-1")
        assert again.status_code == 200
        assert again.json()["cell"]["id"] == first.json()["cell"]["id"]
        assert again.json()["cell"]["line"] == 2
        assert again.json()["relocated_from"] == 1
        assert sorted(cells(s)) == [1, 2]
        assert len(all_sets(s)) == 1

    def test_coach_replay_lands_once(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        first = coach_write(client, s, "brace harder", intent="new", token="c-1")
        assert first.json()["relocated_from"] == 1
        n_actions = PlanAction.objects.filter(plan=s.plan).count()
        again = coach_write(client, s, "brace harder", intent="new", token="c-1")
        assert again.status_code == 200
        assert again.json()["cell"]["id"] == first.json()["cell"]["id"]
        assert again.json()["relocated_from"] == 1
        assert sorted(cells(s)) == [1, 2]
        assert PlanAction.objects.filter(plan=s.plan).count() == n_actions

    def test_a_replay_after_undo_does_not_match_the_restored_line(self, client):
        # The coach's new line lands on a blank line that existed before (a
        # cleared cue); undo puts the blank back. A late retry of that write
        # must write again, not report the blank line as where it landed.
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "old cue")
        coach_write(client, s, "")  # line 1 is now a blank, existing cue
        first = coach_write(client, s, "brace harder", intent="new", token="u-1")
        assert first.json()["cell"]["line"] == 1
        undo(client, s)
        assert sub_cell(s.squat, 1).text == ""
        again = coach_write(client, s, "brace harder", intent="new", token="u-1")
        assert again.status_code == 200
        assert sub_cell(s.squat, 1).text == "brace harder"

    def test_a_different_token_with_the_same_text_still_relocates(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        lines = []
        for token in ["a", "b"]:
            resp = cell_new(client, s, 1, "225 x 5", new=True, token=token)
            assert resp.status_code == 200
            lines.append(resp.json()["cell"]["line"])
        assert lines == [2, 3]
        assert [c.text for c in cells(s).values()] == ["100 x 5", "225 x 5", "225 x 5"]
        assert len(all_sets(s)) == 3

    def test_a_tokenless_replay_keeps_todays_behaviour(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        a = coach_write(client, s, "brace harder", intent="new")
        b = coach_write(client, s, "brace harder", intent="new")
        assert a.json()["cell"]["line"] == 2
        assert b.json()["cell"]["line"] == 3

    def test_a_later_edit_clears_the_token(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        coach_write(client, s, "brace harder", intent="new", token="c-1")
        coach_write(client, s, "brace even harder", line=2)
        assert sub_cell(s.squat, 2).client_token == ""

    @pytest.mark.parametrize("token", ["", 5, "x" * 65])
    def test_a_bad_token_is_a_400(self, client, token):
        s = seed()
        client.force_login(s.coach)
        assert coach_write(client, s, "x", intent="new", token=token).status_code == 400
        client.force_login(s.athlete)
        assert cell_new(client, s, 1, "x", new=True, token=token).status_code == 400


# -- review round 2 ------------------------------------------------------------


class TestTokenIsAHandleForItsLine:
    def test_athlete_same_token_new_text_edits_that_line(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "brace harder")
        client.force_login(s.athlete)
        first = cell_new(client, s, 1, "225 x 5", new=True, token="t")
        assert first.json()["cell"]["line"] == 2
        resp = cell_new(client, s, 1, "230 x 4", new=True, token="t")
        assert resp.status_code == 200
        assert resp.json()["cell"]["line"] == 2
        assert resp.json()["relocated_from"] == 1
        assert sorted(cells(s)) == [1, 2]
        assert cells(s)[2].text == "230 x 4"
        assert cells(s)[2].client_token == "t"
        assert [(r.load, r.reps) for r in all_sets(s)] == [("230", "4")]

    def test_coach_same_token_new_text_edits_that_line(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        first = coach_write(client, s, "brace", intent="new", token="c")
        assert first.json()["cell"]["line"] == 2
        resp = coach_write(client, s, "brace harder", intent="new", token="c")
        assert resp.status_code == 200
        assert resp.json()["cell"]["line"] == 2
        assert resp.json()["relocated_from"] == 1
        assert sorted(cells(s)) == [1, 2]
        assert cells(s)[2].text == "brace harder"
        assert cells(s)[1].text == "225 x 5"

    def test_athlete_line_changed_hands_falls_through_to_relocation(self, client):
        s = seed()
        start(s)
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "100 x 5")
        client.force_login(s.coach)
        first = coach_write(client, s, "100x5", intent="new", token="c")  # line 2
        assert first.json()["cell"]["line"] == 2
        client.force_login(s.athlete)
        assert write_cell(client, s.session, s.squat, 2, "110 x 5").status_code == 200
        client.force_login(s.coach)
        # the coach's stale retry with new text: line 2 is the athlete's now
        resp = coach_write(client, s, "120x5", intent="new", token="c")
        assert resp.status_code == 200
        assert resp.json()["cell"]["line"] == 3
        assert cells(s)[2].text == "110 x 5"
        assert cells(s)[3].text == "120x5"

    # Through the endpoints a claiming edit always clears the token, so these
    # build the "token survived on a line that changed hands" state by ORM (an
    # old-server write during a rolling deploy leaves the column alone). The
    # ownership check is what keeps a handle from editing the other party's line.
    def test_a_coach_token_on_a_line_the_athlete_now_owns_never_edits_it(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        assert (
            coach_write(client, s, "100x5", intent="new", token="h").json()["cell"][
                "line"
            ]
            == 1
        )
        Prescription.objects.filter(pk=sub_cell(s.squat, 1).pk).update(
            text="110 x 5", athlete_authored=True, entered_by_coach=False
        )
        resp = coach_write(client, s, "120x5", intent="new", token="h")
        assert resp.status_code == 200
        assert sub_cell(s.squat, 1).text == "110 x 5"
        assert resp.json()["cell"]["line"] == 2
        assert sub_cell(s.squat, 1).client_token == ""
        assert sub_cell(s.squat, 2).client_token != ""

    def test_an_athlete_token_on_a_line_the_coach_now_owns_never_edits_it(self, client):
        s = seed()
        client.force_login(s.athlete)
        assert (
            cell_new(client, s, 1, "100 x 5", new=True, token="a").json()["cell"][
                "line"
            ]
            == 1
        )
        Prescription.objects.filter(pk=sub_cell(s.squat, 1).pk).update(
            text="brace harder", athlete_authored=False, entered_by_coach=False
        )
        resp = cell_new(client, s, 1, "105 x 5", new=True, token="a")
        assert resp.status_code == 200
        assert sub_cell(s.squat, 1).text == "brace harder"
        assert resp.json()["cell"]["line"] == 2
        assert sub_cell(s.squat, 1).client_token == ""
        assert sub_cell(s.squat, 2).client_token != ""


class TestTokenSurvivesAnUnchangedSave:
    def test_athlete_repair_repost_keeps_the_handle(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "brace harder")
        client.force_login(s.athlete)
        cell_new(client, s, 1, "225 x 5", new=True, token="t")
        assert cell_new(client, s, 2, "225 x 5").status_code == 200  # tokenless
        assert cells(s)[2].client_token == "t"
        again = cell_new(client, s, 1, "225 x 5", new=True, token="t")
        assert again.json()["cell"]["line"] == 2
        assert sorted(cells(s)) == [1, 2]
        assert len(all_sets(s)) == 1

    def test_coach_unchanged_tokenless_post_keeps_the_handle(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        coach_write(client, s, "brace", intent="new", token="c")
        coach_write(client, s, "brace", line=2)
        assert cells(s)[2].client_token == "c"


class TestAthleteNewOntoAnIdenticalCue:
    def test_identical_cue_is_relocated_around(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "225 x 5")  # a cue (session not started)
        client.force_login(s.athlete)
        resp = cell_new(client, s, 1, "225 x 5", new=True)
        assert resp.status_code == 200
        assert resp.json()["relocated_from"] == 1
        assert resp.json()["cell"]["line"] == 2
        assert not cells(s)[1].athlete_authored
        assert cells(s)[2].athlete_authored
        assert len(all_sets(s)) == 1

    def test_identical_coach_set_line_is_left_alone(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        client.force_login(s.athlete)
        resp = cell_new(client, s, 1, "225x5", new=True)
        assert resp.status_code == 200
        assert "relocated_from" not in resp.json()
        assert sorted(cells(s)) == [1]
        assert len(all_sets(s)) == 1


class TestBlankNewLineNeverRelocates:
    def test_coach(self, client):
        s = seed()
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        resp = coach_write(client, s, "", intent="new", token="c")
        assert resp.status_code == 422
        assert resp.json()["code"] == "athlete_line"
        assert sorted(cells(s)) == [1]

    def test_athlete(self, client):
        s = seed()
        client.force_login(s.coach)
        coach_write(client, s, "brace harder")
        client.force_login(s.athlete)
        resp = cell_new(client, s, 1, "", new=True)
        assert resp.status_code == 422
        assert resp.json()["code"] == "coach_line"
        assert sorted(cells(s)) == [1]


class TestTokenClearedWhereTextChanges:
    def _tokened(self, client, s):
        client.force_login(s.athlete)
        write_cell(client, s.session, s.squat, 1, "225 x 5")
        client.force_login(s.coach)
        coach_write(client, s, "brace", intent="new", token="c")
        assert cells(s)[2].client_token == "c"
        return cells(s)[2]

    def test_prescription_patch(self, client):
        s = seed()
        cell = self._tokened(client, s)
        assert patch_text(client, s, cell, "brace harder").status_code == 200
        cell.refresh_from_db()
        assert cell.client_token == ""

    def test_prescription_fill(self, client):
        from store_project.meso.factories import WeekFactory

        s = seed()
        cell = self._tokened(client, s)
        other = WeekFactory(mesocycle=s.meso, index=3)
        Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot, week=other, line=0
        )
        target = Prescription.objects.create(
            exercise_slot=s.squat.exercise_slot,
            week=other,
            line=2,
            text="old",
            client_token="zz",
        )
        resp = client.post(
            reverse(
                "meso:api_prescription_fill",
                kwargs={"plan_id": s.plan.pk, "pk": s.squat.pk},
            ),
            data=json.dumps({"week_ids": [other.pk]}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.text == "brace"
        assert target.client_token == ""
        del cell


class TestPatchLeavesACoachSetToTheWriter:
    def test_changed_text_is_refused(self, client):
        s = seed()
        start(s)
        client.force_login(s.coach)
        coach_write(client, s, "225x5")
        cell = sub_cell(s.squat, 1)
        resp = patch_text(client, s, cell, "315x1")
        assert resp.status_code == 422
        assert resp.json()["code"] == "coach_set"
        cell.refresh_from_db()
        assert cell.text == "225x5"
        assert [(r.load, r.reps) for r in sets_of(s)] == [("225", "5")]
