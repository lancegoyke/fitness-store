"""#717 / #719 — un-skipping re-derives a row's sets; coach actions spare the athlete's.

#717: skipping a row never touches its derived sets, but a coach edit to a
coach set line while the row is skipped clears that line's set, and nothing
brought it back on un-skip (nor on undoing the skip).

#719: a coach action (grid write, undo/redo re-derive, un-skip re-derive)
deletes only a ``LoggedSet`` the COACH entered, never one the athlete entered,
and reaps only a ``SessionLog`` its own set opened.

Tests whose name ends ``_field`` read the new ``entered_by_coach`` /
``opened_by_coach`` columns, so they error (not fail) on a checkout without the
migration; every other test asserts behaviour only.
"""

import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from store_project.analytics.events import EventName
from store_project.analytics.models import Event
from store_project.meso import presenters
from store_project.meso.models import LoggedSet
from store_project.meso.models import PlanAction
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests.test_coach_logs_709 import all_sets
from store_project.meso.tests.test_coach_logs_709 import page_row
from store_project.meso.tests.test_coach_logs_709 import redo
from store_project.meso.tests.test_coach_logs_709 import skip_row
from store_project.meso.tests.test_coach_logs_709 import start
from store_project.meso.tests.test_coach_logs_709 import undo
from store_project.meso.tests.test_parse_at_commit import coach_write
from store_project.meso.tests.test_parse_at_commit import legacy_reclaim
from store_project.meso.tests.test_parse_at_commit import log_post
from store_project.meso.tests.test_parse_at_commit import seed
from store_project.meso.tests.test_parse_at_commit import sub_cell
from store_project.meso.tests.test_parse_at_commit import write_cell

pytestmark = pytest.mark.django_db


def as_coach(client, s):
    client.force_login(s.coach)
    return client


def as_athlete(client, s):
    client.force_login(s.athlete)
    return client


def ok(resp):
    assert resp.status_code == 200, resp.content
    return resp


def athlete_write(client, s, text, exercise=None, line=1):
    as_athlete(client, s)
    return ok(write_cell(client, s.session, exercise or s.squat, line, text))


def sets_of(s, line=1):
    """The sets whose source line is ``line`` (live: a purged cell has none)."""
    return list(
        LoggedSet.objects.filter(
            session_log__session=s.session,
            source_line__exercise_slot=s.squat.exercise_slot,
            source_line__line=line,
        )
    )


def set_events():
    return Event.objects.filter(name=EventName.SET_LOGGED).count()


def values(row):
    return (row.load, row.reps)


def skipped_with_blank_athlete_line(client, s):
    """The athlete's S (225 x 5, line 1) outlives their stale page blanking the line."""
    athlete_write(client, s, "225 x 5")
    (first,) = sets_of(s)
    as_coach(client, s)
    skip_row(client, s, True)
    athlete_write(client, s, "")
    assert [r.pk for r in sets_of(s)] == [first.pk]  # a skipped row is read-only
    as_coach(client, s)
    return first


# -- #717 -----------------------------------------------------------------


class TestUnskipRederivesThePerformanceLines:
    def test_a_coach_set_edited_while_skipped_comes_back(self, client):
        s = seed()
        start(s)
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        assert len(sets_of(s)) == 1
        skip_row(client, s, True)
        ok(coach_write(client, s, "230 x 3", intent="edit"))
        assert sets_of(s) == []  # the writer clears it on a skipped row
        skip_row(client, s, False)
        rows = sets_of(s)
        assert len(rows) == 1
        assert values(rows[0]) == ("230", "3")

    def test_undoing_the_skip_brings_a_coach_set_back(self, client):
        s = seed()
        start(s)
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        skip_row(client, s, True)
        ok(coach_write(client, s, "225 x 5", kind="cue"))
        assert sets_of(s) == []
        undo(client, s)  # the flip: a coach set line again, row still skipped
        assert sets_of(s) == []
        assert sub_cell(s.squat, 1).is_coach_set
        undo(client, s)  # the skip
        assert not Prescription.objects.get(pk=s.squat.pk).skipped
        rows = sets_of(s)
        assert len(rows) == 1
        assert values(rows[0]) == ("225", "5")

    def test_an_athlete_line_typed_while_skipped_gets_its_set_without_an_event(
        self, client
    ):
        s = seed()
        as_coach(client, s)
        skip_row(client, s, True)
        athlete_write(client, s, "225 x 5")
        assert all_sets(s) == []
        events = set_events()
        as_coach(client, s)
        skip_row(client, s, False)
        rows = sets_of(s)
        assert len(rows) == 1
        assert values(rows[0]) == ("225", "5")
        # the athlete did not log it just now
        assert set_events() == events

    def test_guard_an_existing_coach_set_keeps_its_pk(self, client):
        s = seed()
        start(s)
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        (before,) = sets_of(s)
        skip_row(client, s, True)
        skip_row(client, s, False)
        assert [r.pk for r in sets_of(s)] == [before.pk]

    def test_guard_an_existing_athlete_set_keeps_its_pk(self, client):
        s = seed()
        athlete_write(client, s, "225 x 5")
        (before,) = sets_of(s)
        as_coach(client, s)
        skip_row(client, s, True)
        skip_row(client, s, False)
        assert [r.pk for r in sets_of(s)] == [before.pk]

    def test_guard_a_history_row_is_untouched(self, client):
        s = seed()
        first = skipped_with_blank_athlete_line(client, s)
        athlete_write(client, s, "230 x 3")  # typed text on a skipped row: no set
        assert [r.pk for r in sets_of(s)] == [first.pk]
        as_coach(client, s)
        skip_row(client, s, False)
        rows = {r.pk: r for r in all_sets(s)}
        assert first.pk in rows
        assert values(rows[first.pk]) == ("225", "5")


# -- #719.1 ---------------------------------------------------------------


class TestACoachUndoReapsOnlyALogItsSetOpened:
    def test_an_athlete_started_empty_log_survives_undo_and_redo(self, client):
        s = seed()
        as_athlete(client, s)
        ok(log_post(client, s.session, {"status": "pending", "date": "2020-01-01"}))
        log = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        assert len(all_sets(s)) == 1
        undo(client, s)
        assert all_sets(s) == []
        survivor = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert survivor.pk == log.pk
        assert str(survivor.date) == "2020-01-01"
        redo(client, s)
        again = SessionLog.objects.get(session=s.session, athlete=s.athlete)
        assert again.pk == log.pk
        assert str(again.date) == "2020-01-01"
        assert len(all_sets(s)) == 1

    def test_guard_a_log_the_coach_set_opened_is_reaped_by_its_undo(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        assert SessionLog.objects.filter(session=s.session).count() == 1
        assert len(all_sets(s)) == 1
        undo(client, s)
        assert all_sets(s) == []
        assert not SessionLog.objects.filter(session=s.session).exists()

    def test_a_log_the_athlete_posted_to_since_survives_the_undo(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        log = SessionLog.objects.get(session=s.session)
        as_athlete(client, s)
        ok(log_post(client, s.session, {"status": "pending", "date": "2020-01-01"}))
        as_coach(client, s)
        undo(client, s)
        assert all_sets(s) == []
        assert SessionLog.objects.filter(pk=log.pk).exists()

    def test_a_log_the_athlete_has_worked_in_since_survives_the_undo(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        log = SessionLog.objects.get(session=s.session)
        athlete_write(client, s, "felt fast", exercise=s.rdl)
        assert len(all_sets(s)) == 1  # the note makes no set
        as_coach(client, s)
        undo(client, s)
        assert all_sets(s) == []
        assert SessionLog.objects.filter(pk=log.pk).exists()

    def test_opened_by_coach_field(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        log = SessionLog.objects.get(session=s.session)
        assert log.opened_by_coach is True
        as_athlete(client, s)
        ok(log_post(client, s.session, {"status": "pending", "date": "2020-01-01"}))
        log.refresh_from_db()
        assert log.opened_by_coach is False

    def test_opened_by_coach_is_cleared_by_an_athlete_edit_field(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        athlete_write(client, s, "felt fast", exercise=s.rdl)
        assert SessionLog.objects.get(session=s.session).opened_by_coach is False

    def test_an_athlete_started_log_is_not_coach_opened_field(self, client):
        s = seed()
        start(s)
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        assert SessionLog.objects.get(session=s.session).opened_by_coach is False


# -- #719.2 ---------------------------------------------------------------


class TestACoachActionNeverDeletesAnAthleteEnteredSet:
    def test_a_coach_write_over_the_same_values_adopts_the_set_and_its_undo_spares_it(
        self, client
    ):
        s = seed()
        first = skipped_with_blank_athlete_line(client, s)
        skip_row(client, s, False)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        assert [r.pk for r in sets_of(s)] == [first.pk]  # no twin
        undo(client, s)
        assert [r.pk for r in all_sets(s)] == [first.pk]

    def test_a_flip_over_a_legacy_reclaim_spares_the_athletes_set(self, client):
        s = seed()
        athlete_write(client, s, "225 x 5")
        (first,) = sets_of(s)
        legacy_reclaim(s, text="225 x 5")
        as_coach(client, s)
        ok(coach_write(client, s, "225 x 5", kind="set"))
        assert [r.pk for r in sets_of(s)] == [first.pk]
        undo(client, s)
        assert first.pk in [r.pk for r in all_sets(s)]

    def test_different_values_make_the_athletes_set_history_beside_the_coachs(
        self, client
    ):
        s = seed()
        first = skipped_with_blank_athlete_line(client, s)
        skip_row(client, s, False)
        ok(coach_write(client, s, "230 x 3", intent="new"))
        rows = sets_of(s)
        assert sorted(values(r) for r in rows) == [("225", "5"), ("230", "3")]
        entry = next(x for x in page_row(s)["sub_lines"] if x["line"] == 1)
        assert entry["text"] == "230 x 3"
        readonly = page_row(s)["logged_readonly"]
        assert [r["id"] for r in readonly] == [first.pk]
        assert "225" in readonly[0]["label"]
        assert presenters.athlete_set_progress(s.session, s.athlete)["logged"] == 2
        undo(client, s)
        assert [r.pk for r in all_sets(s)] == [first.pk]

    def test_blanking_a_coach_line_backed_by_the_athletes_set_spares_it(self, client):
        s = seed()
        first = skipped_with_blank_athlete_line(client, s)
        skip_row(client, s, False)
        ok(coach_write(client, s, "225 x 5", intent="new"))
        assert [r.pk for r in sets_of(s)] == [first.pk]
        ok(coach_write(client, s, "", intent="edit"))
        assert [r.pk for r in sets_of(s)] == [first.pk]

    def test_entered_by_coach_field(self, client):
        s = seed()
        athlete_write(client, s, "225 x 5")
        as_coach(client, s)
        ok(coach_write(client, s, "230 x 3", intent="new", line=2))
        assert [r.entered_by_coach for r in sets_of(s, 1)] == [False]
        assert [r.entered_by_coach for r in sets_of(s, 2)] == [True]


@pytest.mark.django_db(transaction=True)
def test_migration_0065_backfills_entered_by_coach_field():
    executor = MigrationExecutor(connection)
    leaf_nodes = executor.loader.graph.leaf_nodes("meso")
    before = ("meso", "0064_prescription_entered_by_coach_709")
    after = ("meso", "0065_coach_set_attribution_719")
    try:
        # Seeded BEFORE rolling back, like the 0063 test: the current
        # ``Prescription`` has columns the rolled-back schema may lack.
        s = seed()
        slot = s.squat.exercise_slot
        coach_line = Prescription.objects.create(
            exercise_slot=slot,
            week=s.week,
            line=1,
            text="225 x 5",
            athlete_authored=True,
            entered_by_coach=True,
        )
        athlete_line = Prescription.objects.create(
            exercise_slot=slot,
            week=s.week,
            line=2,
            text="230 x 3",
            athlete_authored=True,
            entered_by_coach=False,
        )
        log = SessionLog.objects.create_for_pair(
            s.session, s.athlete, date=timezone.localdate()
        )
        executor.migrate([before])
        executor.loader.build_graph()
        Historical = executor.loader.project_state([before]).apps.get_model(
            "meso", "LoggedSet"
        )

        def make(line, n):
            return Historical.objects.create(
                session_log_id=log.pk,
                prescription_id=s.squat.pk,
                exercise_slot_id=slot.pk,
                source_line_id=line.pk,
                set_number=n,
                reps="5",
                load="225",
            ).pk

        coach_row = make(coach_line, 1)
        athlete_row = make(athlete_line, 2)

        executor = MigrationExecutor(connection)
        executor.migrate([after])
        executor.loader.build_graph()
        assert LoggedSet.objects.get(pk=coach_row).entered_by_coach is True
        assert LoggedSet.objects.get(pk=athlete_row).entered_by_coach is False
        assert SessionLog.objects.get(pk=log.pk).opened_by_coach is False
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(leaf_nodes)


# -- #719.3 ---------------------------------------------------------------


class TestOnlyALineZeroCanBeSkipped:
    def test_a_sub_line_is_refused(self, client):
        s = seed()
        as_coach(client, s)
        ok(coach_write(client, s, "tempo 3-1-1"))
        sub = sub_cell(s.squat, 1)
        actions = PlanAction.objects.filter(plan=s.plan).count()
        resp = client.post(
            reverse(
                "meso:api_prescription_skip",
                kwargs={"plan_id": s.plan.pk, "pk": sub.pk},
            ),
            data=json.dumps({"skipped": True}),
            content_type="application/json",
        )
        assert resp.status_code == 422, resp.content
        assert resp.json()["code"] == "not_line_zero"
        sub.refresh_from_db()
        assert sub.skipped is False
        assert PlanAction.objects.filter(plan=s.plan).count() == actions
