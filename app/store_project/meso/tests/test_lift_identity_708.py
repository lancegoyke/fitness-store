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
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from store_project.meso import one_rm as meso_one_rm
from store_project.meso import personal_records as meso_prs
from store_project.meso import serializers
from store_project.meso.agent.apply import _apply_swap
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteOneRm
from store_project.meso.models import CoachAthlete
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
