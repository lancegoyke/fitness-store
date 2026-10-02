"""#708 — a logged set keeps the lift the athlete actually did.

``LoggedSet`` resolves its lift through the block-shared ``ExerciseSlot`` LIVE, so
an agent swap or a coach rename after the athlete logged relabels their history.
These tests pin the contract: history reads (1RM, PRs, "last time" labels, agent
grounding) group by the lift stamped at log time, not by the slot's current name.

Scenario: Back Squat 140 kg x 5 (Epley ~163.33) logged in week 1 on day 1; day 2
carries a second live "Back Squat" row. Day 1's row is then swapped/renamed to
Front Squat. Sets are logged the production way (``athlete_cell_write``).
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from store_project.exercises.factories import ExerciseFactory
from store_project.meso import one_rm as meso_one_rm
from store_project.meso import personal_records as meso_prs
from store_project.meso import presenters
from store_project.meso import serializers
from store_project.meso import settle
from store_project.meso.agent.apply import _apply_swap
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import LoggedSetFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import SessionLogFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.lift_identity import Lift
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import CoachAthlete
from store_project.meso.models import LoggedSet
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.models import SessionLog
from store_project.meso.models import Unit
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db

BACK = 140 * (1 + 5 / 30)  # 163.33
FRONT = 80 * (1 + 5 / 30)  # 93.33


def seed_708():
    coach, athlete = UserFactory(), UserFactory()
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(relationship=rel, status=Plan.Status.ACTIVE, unit=Unit.KILOGRAMS)
    meso = MesocycleFactory(plan=plan, name="Block", order=0)
    now = timezone.now()
    w1 = WeekFactory(mesocycle=meso, index=1, delivered_at=now)
    w2 = WeekFactory(mesocycle=meso, index=2, delivered_at=now)
    d1w1 = day(w1, day_number=1, name="Lower A")
    d1w2 = day(w2, session_slot=d1w1.session_slot)
    d2w1 = day(w1, day_number=2, name="Lower B")
    day(w2, session_slot=d2w1.session_slot)
    row1_w1 = presc(d1w1, name="Back Squat", order=0, text="3 x 5")
    slot1 = row1_w1.exercise_slot
    row1_w2 = presc(exercise_slot=slot1, week=w2, text="3 x 5")
    row2_w1 = presc(d2w1, name="Back Squat", order=0, text="3 x 5")
    row2_w2 = presc(exercise_slot=row2_w1.exercise_slot, week=w2, text="3 x 5")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        plan=plan,
        d1w1=d1w1,
        d1w2=d1w2,
        d2w1=d2w1,
        row1_w1=row1_w1,
        row1_w2=row1_w2,
        row2_w1=row2_w1,
        row2_w2=row2_w2,
        slot1=slot1,
    )


def type_line(client, session, cell, text, line=1):
    """Production typed path: the athlete commits a sub-line under a row."""
    return client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": session.pk}),
        data=json.dumps({"exercise_id": cell.pk, "line": line, "text": text}),
        content_type="application/json",
    )


def finish(session, athlete):
    SessionLog.objects.filter(session=session, athlete=athlete).update(
        status=SessionLog.Status.DONE
    )


def logged_week1(client, s):
    client.force_login(s.athlete)
    assert type_line(client, s.d1w1, s.row1_w1, "140 x 5").status_code == 200
    finish(s.d1w1, s.athlete)


def swap(s, name="Front Squat"):
    _apply_swap(SimpleNamespace(pk=1, kind="swap", prescription=s.row1_w1), name)


def rename(client, s, name="Front Squat"):
    client.force_login(s.coach)
    resp = client.post(
        reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": s.plan.pk, "pk": s.row1_w1.pk},
        ),
        data=json.dumps({"name": name}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    client.force_login(s.athlete)


def log_week2_front(client, s):
    assert type_line(client, s.d1w2, s.row1_w2, "80 x 5").status_code == 200
    finish(s.d1w2, s.athlete)


def fresh(cell):
    return type(cell).objects.select_related("exercise_slot").get(pk=cell.pk)


def refresh(s, cells):
    meso_one_rm.refresh_one_rms(s.athlete, cells, Unit.KILOGRAMS)


def one_rms(s):
    return {
        r.key: float(r.value) for r in AthleteOneRm.objects.filter(athlete=s.athlete)
    }


def prs(s):
    return meso_prs.personal_records(s.athlete, unit=Unit.KILOGRAMS)


def assert_one_rm(s, client):
    log_week2_front(client, s)
    refresh(s, [fresh(s.row1_w2)])
    refresh(s, [fresh(s.row2_w2)])
    got = one_rms(s)
    front = got.get("name:front squat")
    assert front == pytest.approx(FRONT, abs=0.05), (
        f"Front Squat 1RM came out of back-squat numbers: {got}"
    )
    assert got.get("name:back squat") == pytest.approx(BACK, abs=0.05), got


def assert_prs(s):
    got = prs(s)
    assert "name:front squat" not in got, (
        f"history relabelled: Front Squat PR {[(k, r.name, r.e1rm) for k, r in got.items()]}"
    )
    assert "name:back squat" in got, f"Back Squat PR vanished: {list(got)}"
    assert got["name:back squat"].e1rm == pytest.approx(BACK, abs=0.05)


def assert_labels(s):
    cells = [fresh(s.row1_w2), fresh(s.row2_w2)]  # fresh: the rename went via HTTP
    labels = serializers.last_logged_labels(s.plan, cells, Unit.KILOGRAMS)
    assert s.row1_w2.pk not in labels, (
        f"Front Squat shows a back-squat 'last time': {labels.get(s.row1_w2.pk)}"
    )
    assert "140" in labels.get(s.row2_w2.pk, ""), labels


def assert_recent(s):
    recent = serializers.serialize_recent_logs(s.plan)
    names = {st["exercise"] for lg in recent for st in lg["sets"]}
    assert names == {"Back Squat"}, f"agent grounding relabelled history: {names}"


class TestAgentSwap:
    def test_one_rm_not_inherited_by_swapped_lift(self, client):
        """The Front Squat %1RM suggestion came out of back-squat numbers."""
        s = seed_708()
        logged_week1(client, s)
        swap(s)
        assert_one_rm(s, client)

    def test_personal_records_keep_back_squat(self, client):
        """The athlete's Back Squat PR turned into a Front Squat PR after a swap."""
        s = seed_708()
        logged_week1(client, s)
        swap(s)
        assert_prs(s)

    def test_last_logged_label_not_shown_on_swapped_row(self, client):
        """The swapped Front Squat row showed 'last time 3x5 140kg' from Back Squat."""
        s = seed_708()
        logged_week1(client, s)
        swap(s)
        assert_labels(s)

    def test_recent_logs_keep_original_label(self, client):
        """The agent was told the athlete front-squatted 140 kg x 5."""
        s = seed_708()
        logged_week1(client, s)
        swap(s)
        assert_recent(s)

    def test_reblur_after_swap_keeps_history(self, client):
        """Re-blurring the old week-1 line after a swap moved the set to Front Squat."""
        s = seed_708()
        logged_week1(client, s)
        swap(s)
        assert type_line(client, s.d1w1, s.row1_w1, "140 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        assert_prs(s)


class TestCoachRename:
    def test_one_rm_not_inherited_by_renamed_lift(self, client):
        """The renamed Front Squat %1RM suggestion came out of back-squat numbers."""
        s = seed_708()
        logged_week1(client, s)
        rename(client, s)
        assert_one_rm(s, client)

    def test_personal_records_keep_back_squat(self, client):
        """A coach rename turned the athlete's Back Squat PR into a Front Squat PR."""
        s = seed_708()
        logged_week1(client, s)
        rename(client, s)
        assert_prs(s)

    def test_last_logged_label_not_shown_on_renamed_row(self, client):
        """The renamed Front Squat row showed 'last time 3x5 140kg' from Back Squat."""
        s = seed_708()
        logged_week1(client, s)
        rename(client, s)
        assert_labels(s)

    def test_recent_logs_keep_original_label(self, client):
        """After a coach rename the agent was told the athlete front-squatted 140 kg."""
        s = seed_708()
        logged_week1(client, s)
        rename(client, s)
        assert_recent(s)


def settle_now(log):
    """Backdate ``log`` past the quiet window and run the sweep's per-log step."""
    SessionLog.objects.filter(pk=log.pk).update(
        last_activity_at=timezone.now() - timedelta(hours=48)
    )
    assert settle.settle_log(log.pk, cutoff=timezone.now() - timedelta(hours=24))


def the_log(session, athlete):
    return SessionLog.objects.get(session=session, athlete=athlete)


class TestSettleAndResults:
    """Red on main: settle and the results PR flag read the slot's NEW name."""

    def test_settle_after_swap_refreshes_the_performed_lift(self, client):
        """A never-finished log that settles after a swap credited Front Squat."""
        s = seed_708()
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "140 x 5").status_code == 200
        assert the_log(s.d1w1, s.athlete).status == SessionLog.Status.PENDING
        swap(s)
        settle_now(the_log(s.d1w1, s.athlete))
        got = one_rms(s)
        assert "name:front squat" not in got, (
            f"settle refreshed the swapped slot's key with back-squat numbers: {got}"
        )
        assert got.get("name:back squat") == pytest.approx(BACK, abs=0.05), got

    def test_results_pr_flag_not_on_swapped_row(self, client):
        """The Back Squat PR was flagged on the row that now reads Front Squat."""
        s = seed_708()
        logged_week1(client, s)  # the first-ever log: a PR
        swap(s)
        results = presenters.session_results(s.d1w1)
        rows = {r["name"]: r for r in results["rows"]}
        assert rows["Front Squat"]["pr"] is False, rows["Front Squat"]
        assert [r["name"] for r in results["summary"]["new_records"]] == [
            "Back Squat"
        ], results["summary"]["new_records"]


# --- guards and write paths (#708 stamping) --------------------------------


def link_slot(cell, exercise):
    slot = cell.exercise_slot
    slot.exercise = exercise
    slot.save(update_fields=["exercise"])


def coach_patch(client, s, cell, payload):
    client.force_login(s.coach)
    resp = client.post(
        reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": s.plan.pk, "pk": cell.pk},
        ),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    client.force_login(s.athlete)


def link_via_coach(client, s, ex):
    coach_patch(client, s, s.row1_w1, {"name": "Back Squat", "exercise_id": str(ex.pk)})


def back_squat_records(s):
    return {k: r for k, r in prs(s).items() if r.name.lower() == "back squat"}


class TestCatalogLinkIsNotASwap:
    """Linking free-text "Back Squat" to the catalog keeps the athlete's history."""

    def test_history_follows_the_link(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        logged_week1(client, s)
        link_via_coach(client, s, ex)
        linked_w2 = fresh(s.row1_w2)
        assert linked_w2.exercise_id == ex.pk

        # Before anything is logged on the linked row: one record, the history.
        records = back_squat_records(s)
        assert len(records) == 1, records
        assert next(iter(records.values())).e1rm == pytest.approx(BACK, abs=0.05)

        labels = serializers.last_logged_labels(s.plan, [linked_w2], Unit.KILOGRAMS)
        assert "140" in labels.get(linked_w2.pk, ""), labels

        refresh(s, [linked_w2])
        assert one_rms(s).get(f"id:{ex.pk}") == pytest.approx(BACK, abs=0.05), one_rms(
            s
        )

        # The first log performed AS the linked lift folds into the same record.
        assert type_line(client, s.d1w2, s.row1_w2, "100 x 5").status_code == 200
        records = back_squat_records(s)
        assert list(records) == [f"id:{ex.pk}"], list(records)
        assert records[f"id:{ex.pk}"].e1rm == pytest.approx(BACK, abs=0.05)
        assert meso_prs.new_records_in(the_log(s.d1w2, s.athlete)) == []

    def test_results_flag_survives_the_link(self, client):
        """Week 1 was the athlete's first Back Squat — still a PR once linked."""
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        logged_week1(client, s)
        link_via_coach(client, s, ex)
        rows = presenters.session_results(s.d1w1)["rows"]
        assert next(r for r in rows if r["name"] == "Back Squat")["pr"] is True

    def test_pending_log_that_settles_after_the_link_folds(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "140 x 5").status_code == 200
        link_via_coach(client, s, ex)
        settle_now(the_log(s.d1w1, s.athlete))
        assert one_rms(s).get(f"id:{ex.pk}") == pytest.approx(BACK, abs=0.05), one_rms(
            s
        )

    def test_unlinking_keeps_history(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        link_slot(s.row1_w1, ex)
        logged_week1(client, s)
        assert LoggedSet.objects.get().exercise_id == ex.pk
        coach_patch(client, s, s.row1_w1, {"name": "Back Squat", "exercise_id": None})
        free_w2 = fresh(s.row1_w2)
        assert free_w2.exercise_id is None

        labels = serializers.last_logged_labels(s.plan, [free_w2], Unit.KILOGRAMS)
        assert "140" in labels.get(free_w2.pk, ""), labels
        records = back_squat_records(s)
        assert len(records) == 1, records
        assert next(iter(records.values())).e1rm == pytest.approx(BACK, abs=0.05)
        refresh(s, [free_w2])
        assert one_rms(s).get("name:back squat") == pytest.approx(BACK, abs=0.05)


class TestSameNameDifferentCatalogRows:
    def test_never_merge(self, client):
        s = seed_708()
        x = ExerciseFactory(name="Back Squat", slug="back-squat-x")
        y = ExerciseFactory(name="Back Squat", slug="back-squat-y")
        link_slot(s.row1_w1, x)
        link_slot(s.row2_w1, y)
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "140 x 5").status_code == 200
        assert type_line(client, s.d2w1, s.row2_w1, "100 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        finish(s.d2w1, s.athlete)

        records = prs(s)
        assert set(records) == {f"id:{x.pk}", f"id:{y.pk}"}, list(records)
        assert records[f"id:{x.pk}"].e1rm == pytest.approx(BACK, abs=0.05)
        assert records[f"id:{y.pk}"].e1rm == pytest.approx(100 * (1 + 5 / 30), abs=0.05)

        only_x = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(x.pk, "Back Squat")], unit=Unit.KILOGRAMS
        )
        assert only_x == {f"id:{x.pk}": pytest.approx(BACK, abs=0.05)}
        only_y = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(y.pk, "Back Squat")], unit=Unit.KILOGRAMS
        )
        assert only_y == {f"id:{y.pk}": pytest.approx(100 * (1 + 5 / 30), abs=0.05)}

        # Documented rule: a free-text target matches every same-named lift.
        free = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(None, "Back Squat")], unit=Unit.KILOGRAMS
        )
        assert free == {"name:back squat": pytest.approx(BACK, abs=0.05)}

    def test_results_pr_flag_is_per_catalog_row(self, client):
        """Y's first-ever record must not flag X's row that merely shares its name."""
        s = seed_708()
        x = ExerciseFactory(name="Back Squat", slug="back-squat-x")
        y = ExerciseFactory(name="Back Squat", slug="back-squat-y")
        link_slot(s.row1_w1, x)
        link_slot(s.row2_w1, x)
        row_y = presc(s.d1w1, name="Back Squat", order=1, text="3 x 5", exercise=y)
        client.force_login(s.athlete)
        # An earlier DONE session sets X's best (140 x 5) ...
        assert type_line(client, s.d2w1, s.row2_w1, "140 x 5").status_code == 200
        finish(s.d2w1, s.athlete)
        # ... so today X (100 x 5) is no record, while Y (100 x 5) is its first.
        assert type_line(client, s.d1w1, s.row1_w1, "100 x 5").status_code == 200
        assert type_line(client, s.d1w1, row_y, "100 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        rows = presenters.session_results(s.d1w1)["rows"]
        flags = [r["pr"] for r in rows]
        assert flags == [False, True], flags


class TestNullStampFallback:
    def test_unstamped_row_reads_as_the_slots_current_identity(self, client):
        s = seed_708()
        logged_week1(client, s)
        LoggedSet.objects.update(exercise=None, exercise_name=None)  # old-code row
        swap(s)
        got = prs(s)
        assert set(got) == {"name:front squat"}, list(got)
        recent = serializers.serialize_recent_logs(s.plan)
        assert {st["exercise"] for lg in recent for st in lg["sets"]} == {"Front Squat"}
        ls = LoggedSet.objects.select_related(
            "exercise_slot", "prescription__exercise_slot"
        ).get()
        assert ls.lift == Lift(None, "Front Squat")


class TestStampWritePaths:
    def test_typed_path_stamps_name_and_catalog_fk(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        link_slot(s.row1_w1, ex)
        logged_week1(client, s)
        ls = LoggedSet.objects.get()
        assert (ls.exercise_id, ls.exercise_name) == (ex.pk, "Back Squat")
        assert ls.lift == Lift(ex.pk, "Back Squat")

    def test_typed_path_stamps_free_text_row(self, client):
        s = seed_708()
        logged_week1(client, s)
        ls = LoggedSet.objects.get()
        assert (ls.exercise_id, ls.exercise_name) == (None, "Back Squat")

    def _log(self, s, cell=None):
        return SessionLogFactory(session=s.d1w1, athlete=s.athlete)

    def test_create_stamps_on_insert(self):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        link_slot(s.row1_w1, ex)
        ls = LoggedSetFactory(session_log=self._log(s), prescription=s.row1_w1)
        ls.refresh_from_db()
        assert (ls.exercise_id, ls.exercise_name) == (ex.pk, "Back Squat")
        bare = LoggedSet.objects.create(
            session_log=ls.session_log,
            prescription=s.row2_w1,
            set_number=9,
            reps="5",
            load="100",
        )
        bare.refresh_from_db()
        assert (bare.exercise_id, bare.exercise_name) == (None, "Back Squat")

    def test_resave_after_rename_does_not_restamp(self):
        s = seed_708()
        ls = LoggedSetFactory(session_log=self._log(s), prescription=s.row1_w1)
        swap(s)
        ls.reps = "8"
        ls.save()
        ls.refresh_from_db()
        assert ls.exercise_name == "Back Squat"
        ls.reps = "9"
        ls.save(update_fields=["reps"])
        ls.refresh_from_db()
        assert (ls.reps, ls.exercise_name) == ("9", "Back Squat")

    def test_repointing_the_prescription_restamps(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        row2_slot = s.row2_w1.exercise_slot
        row2_slot.name, row2_slot.exercise = "Deadlift", ex
        row2_slot.save(update_fields=["name", "exercise"])
        ls = LoggedSetFactory(session_log=self._log(s), prescription=s.row1_w1)
        ls.prescription = s.row2_w1
        ls.save()
        ls.refresh_from_db()
        assert ls.exercise_slot_id == row2_slot.pk
        assert (ls.exercise_id, ls.exercise_name) == (ex.pk, "Deadlift")

    def test_repointing_with_update_fields_adds_the_stamp_fields(self):
        s = seed_708()
        row2_slot = s.row2_w1.exercise_slot
        row2_slot.name = "Deadlift"
        row2_slot.save(update_fields=["name"])
        ls = LoggedSetFactory(session_log=self._log(s), prescription=s.row1_w1)
        ls.prescription = s.row2_w1
        ls.save(update_fields=["prescription"])
        ls.refresh_from_db()
        assert ls.exercise_slot_id == row2_slot.pk
        assert ls.exercise_name == "Deadlift"

    def test_empty_update_fields_writes_nothing(self):
        s = seed_708()
        ls = LoggedSetFactory(session_log=self._log(s), prescription=s.row1_w1)
        ls.reps = "99"
        ls.exercise_name = "Nonsense"
        ls.save(update_fields=[])
        ls.refresh_from_db()
        assert (ls.reps, ls.exercise_name) == ("10", "Back Squat")

    def test_seed_meso_demo_bulk_create_stamps(self):
        from store_project.meso.management.commands import seed_meso_demo

        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        link_slot(s.row1_w1, ex)
        rows = seed_meso_demo.log_typed_sets(
            self._log(s), [(s.row1_w1, ["140 x 5", "130 x 5"])]
        )
        assert len(rows) == 2
        stored = list(LoggedSet.objects.all())
        assert len(stored) == 2
        assert {(r.exercise_id, r.exercise_name) for r in stored} == {
            (ex.pk, "Back Squat")
        }


# --- review round 1 regressions --------------------------------------------


def d2w2(s):
    return Session.objects.get(week=s.d1w2.week, session_slot=s.d2w1.session_slot)


def rename_slot(cell, name):
    slot = cell.exercise_slot
    slot.name = name
    slot.save(update_fields=["name"])


class TestOneCatalogLiftSeveralNames:
    """One catalog lift stamped "Squat" and "Back Squat" showed two different 1RMs."""

    def test_every_name_of_the_fk_sees_the_same_history(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        rename_slot(s.row1_w1, "Squat")
        rename_slot(s.row2_w1, "Squat")
        client.force_login(s.athlete)
        # C: free-text "Squat" 150 x 1.
        assert type_line(client, s.d1w1, s.row1_w1, "150 x 1").status_code == 200
        finish(s.d1w1, s.athlete)
        # Link the row as (E, "Squat"); B: 100 x 3 logged on it.
        coach_patch(client, s, s.row1_w1, {"name": "Squat", "exercise_id": str(ex.pk)})
        assert type_line(client, s.d1w2, s.row1_w2, "100 x 3").status_code == 200
        finish(s.d1w2, s.athlete)
        # A staff rename between picks: another row links as (E, "Back Squat").
        coach_patch(
            client, s, s.row2_w1, {"name": "Back Squat", "exercise_id": str(ex.pk)}
        )
        row_bs = fresh(s.row2_w2)
        assert row_bs.exercise_id == ex.pk
        assert type_line(client, d2w2(s), row_bs, "100 x 5").status_code == 200
        finish(d2w2(s), s.athlete)

        key = f"id:{ex.pk}"
        records = prs(s)
        assert list(records) == [key], list(records)
        assert records[key].e1rm == pytest.approx(150, abs=0.05), (
            f"free-text set missing from the record: {records[key].e1rm}"
        )

        by_back = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(ex.pk, "Back Squat")], unit=Unit.KILOGRAMS
        )
        by_squat = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(ex.pk, "Squat")], unit=Unit.KILOGRAMS
        )
        assert by_back == by_squat == {key: pytest.approx(150, abs=0.05)}, (
            by_back,
            by_squat,
        )

        refresh(s, [row_bs])
        assert one_rms(s).get(key) == pytest.approx(150, abs=0.05), one_rms(s)

    def test_new_record_baseline_includes_the_free_text_set(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        rename_slot(s.row1_w1, "Squat")
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "150 x 1").status_code == 200
        finish(s.d1w1, s.athlete)
        coach_patch(client, s, s.row1_w1, {"name": "Squat", "exercise_id": str(ex.pk)})
        assert type_line(client, s.d1w2, s.row1_w2, "100 x 3").status_code == 200
        finish(s.d1w2, s.athlete)
        coach_patch(
            client, s, s.row2_w1, {"name": "Back Squat", "exercise_id": str(ex.pk)}
        )
        row_bs = fresh(s.row2_w2)
        assert type_line(client, d2w2(s), row_bs, "100 x 5").status_code == 200
        finish(d2w2(s), s.athlete)
        # A later session on the "Back Squat" row: 120 x 1 beats nothing.
        later = day(s.d1w2.week, day_number=9, name="Later")
        row_later = presc(later, name="Back Squat", order=0, text="1 x 1", exercise=ex)
        assert type_line(client, later, row_later, "120 x 1").status_code == 200
        finish(later, s.athlete)
        assert meso_prs.new_records_in(the_log(later, s.athlete)) == []


class TestRefreshCoversCrossMatchingRows:
    """Editing a free-text Back Squat set left the catalog Back Squat 1RM stale."""

    def test_other_stored_rows_are_rederived(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        other = ExerciseFactory(name="Back Squat", slug="back-squat-f")
        link_slot(s.row2_w1, ex)
        deadlift = presc(s.d2w1, name="Deadlift", order=1, text="3 x 5")
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "200 x 1").status_code == 200
        assert type_line(client, s.d2w1, deadlift, "100 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        finish(s.d2w1, s.athlete)
        refresh(s, [fresh(s.row1_w1), fresh(deadlift)])
        assert one_rms(s)["name:back squat"] == pytest.approx(200, abs=0.05)

        assert type_line(client, s.d2w1, s.row2_w1, "100 x 5").status_code == 200
        finish(s.d2w1, s.athlete)
        refresh(s, [fresh(s.row2_w1)])
        key = f"id:{ex.pk}"
        assert one_rms(s)[key] == pytest.approx(200, abs=0.05), one_rms(s)

        manual = AthleteOneRm.objects.create(
            athlete=s.athlete,
            exercise=other,
            name="Back Squat",
            key=f"id:{other.pk}",
            value=999,
            unit=Unit.KILOGRAMS,
            source=AthleteOneRm.Source.MANUAL,
        )
        dl = AthleteOneRm.objects.get(athlete=s.athlete, key="name:deadlift")
        dl_before = (dl.pk, dl.value, dl.updated_at)

        with CaptureQueriesContext(connection) as ctx:
            assert type_line(client, s.d1w1, s.row1_w1, "100 x 1").status_code == 200

        want = 100 * (1 + 5 / 30)
        got = one_rms(s)
        assert got[key] == pytest.approx(want, abs=0.05), (
            f"catalog Back Squat 1RM stale after the free-text edit: {got}"
        )
        assert got["name:back squat"] == pytest.approx(want, abs=0.05), got

        dl.refresh_from_db()
        assert (dl.pk, dl.value, dl.updated_at) == dl_before
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("UPDATE", "DELETE"))
            and "meso_athleteonerm" in q["sql"]
            and q["sql"].rstrip().endswith(f'."id" = {dl.pk}')
        ]
        assert writes == [], writes

        manual.refresh_from_db()
        assert float(manual.value) == 999
        assert manual.source == AthleteOneRm.Source.MANUAL


def cell_on(s, name, exercise=None):
    """A second slot's cell under ``name`` (the re-point target)."""
    rename_slot(s.row2_w1, name)
    if exercise is not None:
        link_slot(s.row2_w1, exercise)
    return fresh(s.row2_w1)


def stamped(row):
    row.refresh_from_db()
    return (row.exercise_id, row.exercise_name)


class TestRepointAgainstLoadedAnchor:
    """Moving a logged set to another row left it counted under the old lift."""

    def _row(self, s):
        log = SessionLogFactory(session=s.d1w1, athlete=s.athlete)
        ls = LoggedSetFactory(session_log=log, prescription=s.row1_w1)
        assert stamped(ls) == (None, "Back Squat")
        return log, LoggedSet.objects.get(pk=ls.pk)

    def test_caller_assigned_slot_is_caught(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        target = cell_on(s, "Deadlift", ex)
        _, row = self._row(s)
        row.prescription = target
        row.exercise_slot = target.exercise_slot
        row.save()
        assert stamped(row) == (ex.pk, "Deadlift")

    def test_caller_assigned_slot_with_update_fields(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        target = cell_on(s, "Deadlift", ex)
        _, row = self._row(s)
        row.prescription = target
        row.exercise_slot = target.exercise_slot
        row.save(update_fields=["prescription", "exercise_slot"])
        assert stamped(row) == (ex.pk, "Deadlift")

    def test_unanchored_row_gets_stamped_when_anchored(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        target = cell_on(s, "Deadlift", ex)
        log = SessionLogFactory(session=s.d1w1, athlete=s.athlete)
        made = LoggedSet.objects.create(
            session_log=log, set_number=9, reps="5", load="100"
        )
        assert stamped(made) == (None, None)
        row = LoggedSet.objects.get(pk=made.pk)
        row.prescription = target
        row.save()
        assert stamped(row) == (ex.pk, "Deadlift")

    def test_row_loaded_without_a_slot_repoints_on_prescription(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        target = cell_on(s, "Deadlift", ex)
        _, row = self._row(s)
        LoggedSet.objects.filter(pk=row.pk).update(exercise_slot=None)
        row = LoggedSet.objects.get(pk=row.pk)
        assert row.exercise_slot_id is None
        row.prescription = target
        row.save()
        assert stamped(row) == (ex.pk, "Deadlift")

    def test_plain_resave_after_rename_keeps_the_stamp(self):
        s = seed_708()
        _, row = self._row(s)
        rename_slot(s.row1_w1, "Front Squat")
        row = LoggedSet.objects.get(pk=row.pk)
        row.reps = "8"
        row.save()
        assert stamped(row) == (None, "Back Squat")


class TestCasefoldNames:
    """A "Fußheben" set was missed by a target spelled "FUSSHEBEN"."""

    def test_full_casefold_matches(self, client):
        s = seed_708()
        rename_slot(s.row1_w1, "Fußheben")
        rename_slot(s.row2_w1, "Fußheben")
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "100 x 5").status_code == 200
        assert type_line(client, s.d2w1, s.row2_w1, "80 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        finish(s.d2w1, s.athlete)
        LoggedSet.objects.filter(session_log__session=s.d1w1).update(
            exercise_name="Fußheben"
        )
        LoggedSet.objects.filter(session_log__session=s.d2w1).update(
            exercise_name="FUSSHEBEN"
        )
        got = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(None, "FUSSHEBEN")], unit=Unit.KILOGRAMS
        )
        assert list(got.values()) == [pytest.approx(100 * (1 + 5 / 30), abs=0.05)], got
        records = prs(s)
        assert len(records) == 1, [(k, r.name) for k, r in records.items()]


# --- review round 2 regressions --------------------------------------------


def link_row(client, s, cell, ex, name):
    coach_patch(client, s, cell, {"name": name, "exercise_id": str(ex.pk)})


class TestCanonicalCatalogEstimate:
    """``id:<pk>`` holds one value, whichever row (or leftover stamp) asked."""

    def test_names_come_from_live_rows_not_the_target(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        rename_slot(s.row1_w1, "Squat")
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "150 x 1").status_code == 200
        finish(s.d1w1, s.athlete)
        # Row 1 is now linked as (E, "Squat") but nothing is logged under it.
        link_row(client, s, s.row1_w1, ex, "Squat")
        link_row(client, s, s.row2_w1, ex, "Back Squat")
        row_bs = fresh(s.row2_w2)
        assert type_line(client, d2w2(s), row_bs, "100 x 5").status_code == 200
        finish(d2w2(s), s.athlete)

        key = f"id:{ex.pk}"
        refresh(s, [row_bs])
        assert one_rms(s).get(key) == pytest.approx(150, abs=0.05), (
            f"free-text 'Squat' set missing from the stored estimate: {one_rms(s)}"
        )
        by_back = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(ex.pk, "Back Squat")], unit=Unit.KILOGRAMS
        )
        by_squat = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(ex.pk, "Squat")], unit=Unit.KILOGRAMS
        )
        assert by_back == by_squat == {key: pytest.approx(150, abs=0.05)}, (
            by_back,
            by_squat,
        )

    def test_deleted_stamp_name_does_not_decide_the_estimate(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        rename_slot(s.row1_w1, "Squat")
        client.force_login(s.athlete)
        # Free-text "Squat" 200 x 1.
        assert type_line(client, s.d1w1, s.row1_w1, "200 x 1").status_code == 200
        finish(s.d1w1, s.athlete)
        # Linked as (E, "Squat"); a week-2 set is stamped (E, "Squat").
        link_row(client, s, s.row1_w1, ex, "Squat")
        assert type_line(client, s.d1w2, s.row1_w2, "100 x 1").status_code == 200
        finish(s.d1w2, s.athlete)
        # Row 2: (E, "Back Squat") 150 x 1.
        link_row(client, s, s.row2_w1, ex, "Back Squat")
        assert type_line(client, s.d2w1, s.row2_w1, "150 x 1").status_code == 200
        finish(s.d2w1, s.athlete)
        # No live row is (E, "Squat") any more.
        link_row(client, s, s.row1_w1, ex, "Back Squat")
        # The athlete clears the only set stamped (E, "Squat").
        assert type_line(client, s.d1w2, fresh(s.row1_w2), "").status_code == 200
        assert not LoggedSet.objects.filter(exercise_name="Squat", exercise=ex).exists()
        assert the_log(s.d1w2, s.athlete).status == SessionLog.Status.DONE

        key = f"id:{ex.pk}"
        assert one_rms(s).get(key) == pytest.approx(150, abs=0.05), (
            f"stale name from the deleted stamp decided the estimate: {one_rms(s)}"
        )
        canonical = meso_one_rm.derive_one_rm_values(
            s.athlete, lifts=[Lift(ex.pk, "Back Squat")], unit=Unit.KILOGRAMS
        )
        assert one_rms(s)[key] == pytest.approx(canonical[key], abs=0.05), canonical


class TestOtherRowsCompareAndSet:
    def _seed(self, client):
        s = seed_708()
        ex = ExerciseFactory(name="Back Squat", slug="back-squat")
        link_slot(s.row2_w1, ex)
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "200 x 1").status_code == 200
        assert type_line(client, s.d2w1, s.row2_w1, "100 x 5").status_code == 200
        finish(s.d1w1, s.athlete)
        finish(s.d2w1, s.athlete)
        refresh(s, [fresh(s.row1_w1)])
        refresh(s, [fresh(s.row2_w1)])
        key = f"id:{ex.pk}"
        row = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert float(row.value) == pytest.approx(200, abs=0.05)
        assert row.source == AthleteOneRm.Source.LOGGED
        return s, key

    def test_concurrent_manual_estimate_survives(self, client, monkeypatch):
        s, key = self._seed(client)
        real = meso_one_rm.derive_one_rm_values

        def racing(*args, **kwargs):
            # Lands after ``others`` was read, before the re-derive writes.
            AthleteOneRm.objects.filter(athlete=s.athlete, key=key).update(
                source=AthleteOneRm.Source.MANUAL, value=250
            )
            return real(*args, **kwargs)

        monkeypatch.setattr(meso_one_rm, "derive_one_rm_values", racing)
        assert type_line(client, s.d1w1, s.row1_w1, "100 x 1").status_code == 200
        row = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert (row.source, float(row.value)) == (AthleteOneRm.Source.MANUAL, 250.0), (
            row.source,
            row.value,
        )

    def test_stale_other_row_is_updated_and_touched(self, client):
        s, key = self._seed(client)
        before = AthleteOneRm.objects.get(athlete=s.athlete, key=key).updated_at
        assert type_line(client, s.d1w1, s.row1_w1, "100 x 1").status_code == 200
        row = AthleteOneRm.objects.get(athlete=s.athlete, key=key)
        assert float(row.value) == pytest.approx(100 * (1 + 5 / 30), abs=0.05)
        assert row.source == AthleteOneRm.Source.LOGGED
        assert row.updated_at > before


class TestPrFlagOnWinningStamp:
    def test_free_text_winner_keeps_its_flag_after_a_link(self, client):
        s = seed_708()
        x = ExerciseFactory(name="Back Squat", slug="back-squat-x")
        y = ExerciseFactory(name="Back Squat", slug="back-squat-y")
        row_b = presc(s.d1w1, name="Back Squat", order=1, text="3 x 5", exercise=x)
        client.force_login(s.athlete)
        assert type_line(client, s.d1w1, s.row1_w1, "200 x 1").status_code == 200
        assert type_line(client, s.d1w1, row_b, "100 x 1").status_code == 200
        finish(s.d1w1, s.athlete)
        # Only row A (the free-text winner) moves to catalog lift Y.
        link_row(client, s, s.row1_w1, y, "Back Squat")
        rows = presenters.session_results(s.d1w1)["rows"]
        flags = [r["pr"] for r in rows]
        assert flags == [True, True], (flags, [r["name"] for r in rows])


class TestRepointNeedsAnchorWrite:
    def _setup(self):
        s = seed_708()
        ex = ExerciseFactory(name="Deadlift", slug="deadlift")
        target = cell_on(s, "Deadlift", ex)
        log = SessionLogFactory(session=s.d1w1, athlete=s.athlete)
        made = LoggedSetFactory(session_log=log, prescription=s.row1_w1)
        row = LoggedSet.objects.get(pk=made.pk)
        sa, sb = s.row1_w1.exercise_slot_id, target.exercise_slot_id
        assert row.exercise_slot_id == sa != sb
        return s, ex, target, row, sa, sb

    @staticmethod
    def _db(row):
        return LoggedSet.objects.values_list(
            "exercise_slot_id", "prescription_id", "exercise_id", "exercise_name"
        ).get(pk=row.pk)

    def test_prescription_only_save_moves_the_anchor_with_the_stamp(self):
        s, ex, target, row, sa, sb = self._setup()
        row.prescription = target
        row.exercise_slot = target.exercise_slot
        row.save(update_fields=["prescription"])
        assert self._db(row) == (sb, target.pk, ex.pk, "Deadlift")

    def test_save_that_skips_the_anchor_does_not_repoint(self):
        s, ex, target, row, sa, sb = self._setup()
        before = self._db(row)
        row.prescription = target
        row.exercise_slot = target.exercise_slot
        row.reps = "7"
        row.save(update_fields=["reps"])
        assert self._db(row)[:] == before, self._db(row)
        assert LoggedSet.objects.get(pk=row.pk).reps == "7"
        # A later full save writes the anchor, so it re-points then.
        row.save()
        assert self._db(row) == (sb, target.pk, ex.pk, "Deadlift")


class TestRecentLogsOrdinalsFollowTheLift:
    """#716: the agent saw "Front Squat, set 3" for the first front-squat set."""

    def test_sets_are_numbered_within_the_stamped_lift(self):
        s = seed_708()
        log = SessionLogFactory(session=s.d1w1, athlete=s.athlete)
        for n in (1, 2):
            LoggedSetFactory(session_log=log, prescription=s.row1_w1, set_number=n)
        swap(s)  # mid-session: the same row is now Front Squat
        LoggedSetFactory(session_log=log, prescription=s.row1_w1, set_number=3)
        sets = serializers.serialize_recent_logs(s.plan)[0]["sets"]
        assert [(x["exercise"], x["set"]) for x in sets] == [
            ("Back Squat", 1),
            ("Back Squat", 2),
            ("Front Squat", 1),
        ]

    def test_set_ordinals_default_stays_per_row(self):
        """The athlete and results surfaces keep numbering per row (not red on main)."""
        s = seed_708()
        log = SessionLogFactory(session=s.d1w1, athlete=s.athlete)
        a = LoggedSetFactory(session_log=log, prescription=s.row1_w1, set_number=1)
        swap(s)
        b = LoggedSetFactory(session_log=log, prescription=s.row1_w1, set_number=2)
        assert serializers.set_ordinals([a, b]) == {a.pk: 1, b.pk: 2}
