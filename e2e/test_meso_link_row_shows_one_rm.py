"""Renaming a row to a lift the athlete has history for shows its 1RM at once (#715).

The athlete's stored 1RM for a row used to be recomputed only when they
finished a session, so a coach who renamed, swapped or catalog-linked a row
left the athlete's %1RM suggestion blank until their next finished session.
The server now refreshes it as part of the coach's edit.

Journey: the athlete logs "140 x 5" on week-1 Back Squat and never finishes
or settles it; the log is marked DONE directly, so no stored 1RM exists yet.
Week 1 also has a row "Pause Squat" (no history) prescribed at 75% in week 2. The coach renames "Pause Squat" to "Back Squat" in the designer. With
no new athlete session finished, the athlete's week-2 card must already
suggest 122.5.

The catalog-link variant is covered server-side only: the designer's name
suggestions hide a catalog entry whose name equals the coach's own row name,
so a same-name link cannot be picked through the UI.

Desktop only: the designer is a desktop surface.
"""

from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import AthleteOneRm
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
    pause1 = presc(lower1, name="Pause Squat", order=1, text="3 x 5 @ 100")
    lower2 = day(week2, day_number=1, session_slot=lower1.session_slot)
    pause2 = presc(
        lower2,
        order=1,
        exercise_slot=pause1.exercise_slot,
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
        pause1=pause1,
        pause2=pause2,
    )


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_a_rename_shows_the_one_rm_without_a_new_session(
    page, new_page, live_server, shot, press, login, two_week_plan
):
    p = two_week_plan
    # --- 1. athlete types "140 x 5" on week-1 Back Squat, never Finish ---
    login(p.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": p.lower1.pk}))
    card = page.get_by_test_id("exercise-card").filter(has_text="Back Squat")
    first_line = card.get_by_test_id("sub-line-input").first
    press(first_line)
    first_line.fill("140 x 5")
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/cell/" in r.url
    ) as cell_response_info:
        first_line.press("Tab")
    assert cell_response_info.value.ok

    log = SessionLog.objects.get(session=p.lower1, athlete=p.athlete)
    assert log.status == SessionLog.Status.PENDING
    # The log counts as DONE history, but no finish or settle ran, so no stored
    # AthleteOneRm exists yet (the unit tests' `finish` helper does the same).
    SessionLog.objects.filter(pk=log.pk).update(status=SessionLog.Status.DONE)
    assert not AthleteOneRm.objects.filter(athlete=p.athlete).exists()

    # --- 2. the coach renames "Pause Squat" to "Back Squat" in the designer ---
    coach_page = new_page(desktop=True)
    login(p.coach, on=coach_page)
    coach_page.goto(
        f"{live_server.url}"
        f"{reverse('meso:designer_plan', kwargs={'plan_id': p.plan.pk})}"
    )
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    slot = p.pause1.exercise_slot
    name = coach_page.get_by_test_id(f"row-name-{slot.pk}")
    name.click()
    coach_page.keyboard.press("ControlOrMeta+A")
    coach_page.keyboard.type("Back Squat")
    name.press("Tab")
    coach_page.wait_for_load_state("networkidle")
    slot.refresh_from_db()
    assert slot.name == "Back Squat"

    # --- 3. week 2: the athlete sees the load with no new session ---
    page.goto(reverse("meso:athlete_session", kwargs={"pk": p.lower2.pk}))
    squat = page.get_by_test_id("exercise-card").filter(has_text="Back Squat")
    expect(squat).to_have_count(1)
    expect(squat.locator(".meso-badge", has_text="%1RM")).to_be_visible()
    # Epley(140, 5) = 163.33; 75% of it rounds to 122.5.
    expect(squat.get_by_text("≈ 122.5")).to_be_visible()
    expect(squat.get_by_text("from your logs")).to_be_visible()
    shot("01-week2-back-squat")
