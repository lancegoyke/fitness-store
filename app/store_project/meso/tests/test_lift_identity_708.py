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
