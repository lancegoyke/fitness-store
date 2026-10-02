"""A rename after logging must not hand the back-squat 1RM to Front Squat (#708).

A `LoggedSet` used to resolve its lift through the block-shared `ExerciseSlot`,
live. Once the coach renamed the row (or the agent swapped it) the athlete's
past back squats became Front Squat history, so the Front Squat estimated 1RM
came from back-squat numbers and the %1RM logger suggested a far-too-heavy
load. The lift is now stamped on each set when it is logged.

Journey: the athlete logs "140 x 5" on week-1 Back Squat (never Finish), the
coach renames the row to Front Squat, the 24h settle runs, then the athlete's
week-2 card (prescribed at 75%) must NOT size a load off that set, and their
records panel must still say Back Squat.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.meso import settle
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_week_plan(db):
    coach = UserFactory(name="Casey Coach", email="casey.coach@example.com")
    athlete = UserFactory(name="Alex Athlete", email="alex.athlete@example.com")
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Strength Block", status=Plan.Status.ACTIVE, unit="kg"
    )
    mesocycle = MesocycleFactory(plan=plan, name="Accumulation", order=0)
    now = timezone.now()
    week1 = WeekFactory(mesocycle=mesocycle, index=1, delivered_at=now)
    week2 = WeekFactory(mesocycle=mesocycle, index=2, delivered_at=now)
    lower1 = day(week1, day_number=1, name="Lower", bias="Squat")
    squat1 = presc(lower1, name="Back Squat", order=0, text="3 x 5 @ 140")
    lower2 = day(week2, day_number=1, session_slot=lower1.session_slot)
    squat2 = presc(
        lower2,
        order=0,
        exercise_slot=squat1.exercise_slot,
        week=week2,
        text="3 x 5 @ 75%",
    )
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        plan=plan,
        lower1=lower1,
        lower2=lower2,
        squat1=squat1,
        squat2=squat2,
    )


def test_a_rename_after_logging_keeps_the_back_squat_history(
    page, viewport, shot, press, login, two_week_plan
):
    p = two_week_plan

    # --- 1. athlete types "140 x 5" on week-1 Back Squat, never Finish ---
    login(p.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": p.lower1.pk}))
    card = page.get_by_test_id("exercise-card").filter(has_text="Back Squat")
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    press(first_line)
    first_line.fill("140 x 5")
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/cell/" in r.url
    ) as cell_response_info:
        if viewport["is_phone"]:
            sub_lines.nth(1).tap()
        else:
            first_line.press("Tab")
    assert cell_response_info.value.ok

    log = SessionLog.objects.get(session=p.lower1, athlete=p.athlete)
    assert log.status == SessionLog.Status.PENDING

    # --- 2. coach renames the row (setup, not the subject) ---
    coach_client = Client()
    coach_client.force_login(p.coach)
    rename = coach_client.post(
        reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": p.plan.pk, "pk": p.squat1.pk},
        ),
        data=json.dumps({"name": "Front Squat"}),
        content_type="application/json",
    )
    assert rename.status_code == 200, rename.content

    # --- 3. the 24h settle runs ---
    cutoff = timezone.now() - settle.quiet_period()
    SessionLog.objects.filter(pk=log.pk).update(
        last_activity_at=cutoff - timedelta(hours=1)
    )
    assert settle.settle_log(log.pk, cutoff=cutoff) is True

    # --- 4. week 2: Front Squat is a %1RM lift with no back-squat suggestion ---
    page.goto(reverse("meso:athlete_session", kwargs={"pk": p.lower2.pk}))
    front = page.get_by_test_id("exercise-card").filter(has_text="Front Squat")
    expect(front).to_have_count(1)
    expect(front.locator(".meso-badge", has_text="%1RM")).to_be_visible()
    one_rm_input = front.locator("input[placeholder]").first
    expect(one_rm_input).to_be_visible()
    # Epley(140, 5) = 163.33; 75% of it rounds to 122.5. Main shows both.
    # (`to_be_hidden`, not `not_to_contain_text`: Alpine keeps the x-show'd
    # spans in the DOM, so a text-content check would see them while hidden.)
    expect(front.get_by_text("≈ 122.5")).to_be_hidden()
    expect(front.get_by_text("from your logs")).to_be_hidden()
    placeholder = one_rm_input.get_attribute("placeholder")
    assert not placeholder.startswith("163"), placeholder
    shot("01-week2-front-squat")

    # --- 5. records: the 1RM still belongs to Back Squat ---
    page.goto(reverse("meso:athlete_home"))
    panel = page.locator(".meso-card").filter(has_text="Personal records")
    expect(panel).to_be_visible()
    back_row = panel.get_by_text("Back Squat", exact=True)
    expect(back_row).to_be_visible()
    expect(panel).to_contain_text("163 kg")
    expect(panel.get_by_text("Front Squat")).to_have_count(0)
    shot("02-records")
