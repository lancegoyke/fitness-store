"""The athlete's Training log outlives the program and the coach (#700).

The log is the athlete's own record. Test 1 proves it survives the two ways a
program normally takes workouts away: the coach deletes a day (the athlete's
sets on it must stay, readable, and reachable from the log AND from a personal
record's provenance link), and the coach ends the relationship (the log and the
records panel must still be there, now labelled with the coach). Both phone
widths also must not scroll sideways on the log list or a workout page.

Test 2 proves the PWA side: with the network cut, a first-time visit to
`/meso/me/log/` lands on the cached offline shell, not a browser net error.
That needs a REAL service worker, which the suite's contexts block, so it
builds its own context.
"""

import json
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.meso.factories import CoachAthleteFactory
from store_project.meso.factories import MesocycleFactory
from store_project.meso.factories import PlanFactory
from store_project.meso.factories import WeekFactory
from store_project.meso.models import CoachAthlete
from store_project.meso.models import Plan
from store_project.meso.models import Session
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc
from store_project.users.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_day_plan(db):
    coach = UserFactory(name="Casey Coach", email="casey.coach@example.com")
    athlete = UserFactory(name="Alex Athlete", email="alex.athlete@example.com")
    rel = CoachAthleteFactory(
        coach=coach, athlete=athlete, status=CoachAthlete.Status.ACTIVE
    )
    plan = PlanFactory(
        relationship=rel, title="Strength Block", status=Plan.Status.ACTIVE, unit="kg"
    )
    mesocycle = MesocycleFactory(plan=plan, name="Accumulation", order=0)
    week = WeekFactory(mesocycle=mesocycle, index=1, delivered_at=timezone.now())
    lower = day(week, day_number=1, name="Lower", bias="Squat")
    squat = presc(lower, name="Back Squat", order=0, text="3 x 5 @ 100")
    upper = day(week, day_number=2, name="Upper", bias="Press")
    presc(upper, name="Bench Press", order=0, text="3 x 5 @ 80")
    return SimpleNamespace(
        coach=coach,
        athlete=athlete,
        rel=rel,
        plan=plan,
        lower=lower,
        squat=squat,
        upper=upper,
    )


def _is_cell_post(response):
    return response.request.method == "POST" and "/cell/" in response.url


def _fits(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def _log_a_line(page, press, card_name, text):
    """Type `text` on the card's first line, blur, and wait for the save."""
    card = page.get_by_test_id("exercise-card").filter(has_text=card_name)
    field = card.get_by_test_id("sub-line-input").first
    press(field)
    field.fill(text)
    with page.expect_response(_is_cell_post) as info:
        press(page.get_by_test_id("session-note-input"))
    assert info.value.ok, info.value.status


@pytest.mark.parametrize("viewport", ["phone", "phone-360"], indirect=True)
def test_athlete_keeps_a_deleted_days_workout_and_reaches_it_after_the_coach_leaves(
    page, viewport, shot, press, login, new_page, two_day_plan
):
    p = two_day_plan
    login(p.athlete)

    # --- 1. the athlete logs both sessions through the UI ---
    for name, line, lift in (
        ("Lower", "100 x 5", "Back Squat"),
        ("Upper", "80 x 5", "Bench Press"),
    ):
        page.goto(reverse("meso:athlete_home"))
        press(page.locator("a.meso-row").filter(has_text=name).first)
        expect(page.get_by_role("heading", name=name)).to_be_visible()
        _log_a_line(page, press, lift, line)

    # --- 2. the coach removes Day 2 in the designer (desktop) ---
    coach_page = new_page(desktop=True)
    login(p.coach, on=coach_page)
    coach_page.goto(reverse("meso:designer_plan", kwargs={"plan_id": p.plan.pk}))
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    slot_id = p.upper.session_slot_id
    coach_page.get_by_test_id(f"remove-day-{slot_id}").click()
    coach_page.get_by_role("button", name="Confirm remove day").click()
    expect(coach_page.get_by_test_id(f"meso-day-table-{slot_id}")).to_have_count(0)
    assert Session.objects.get(pk=p.upper.pk).deleted_at is not None

    # --- 3. the log still has both workouts; the deleted day's is read-only ---
    page.goto(reverse("meso:athlete_home"))
    press(page.get_by_test_id("training-log-link"))
    entries = page.get_by_test_id("training-log-entry")
    expect(entries).to_have_count(2)
    expect(entries.nth(0)).to_contain_text("Upper")
    expect(entries.nth(1)).to_contain_text("Lower")
    assert _fits(page), "the log list scrolls sideways"
    press(entries.nth(0))
    expect(page.get_by_test_id("workout-set")).to_have_text("Set 1 · 80 kg × 5")
    expect(page.get_by_test_id("workout-readonly-note")).to_be_visible()
    expect(page.get_by_test_id("open-session")).to_have_count(0)
    workout_url = page.url
    assert _fits(page), "the workout page scrolls sideways"
    shot("deleted-day-workout")

    # --- 4. the record's provenance link lands on the same workout ---
    page.goto(reverse("meso:athlete_home"))
    bench_link = page.locator(
        "xpath=//span[normalize-space()='Bench Press']/ancestor::div[2]"
        "//a[@data-testid='pr-provenance']"
    )
    press(bench_link)
    expect(page.get_by_test_id("workout-set")).to_have_text("Set 1 · 80 kg × 5")
    assert page.url == workout_url

    # --- 5. the coach ends the relationship ---
    coach_page.goto(reverse("meso:athlete", kwargs={"pk": p.athlete.pk}))
    coach_page.get_by_text("End coaching relationship").click()
    coach_page.get_by_role("button", name="End coaching", exact=True).click()
    p.rel.refresh_from_db()
    assert p.rel.status != CoachAthlete.Status.ACTIVE

    # --- 6. the athlete still has the link, the records, and both workouts ---
    page.goto(reverse("meso:athlete_home"))
    expect(page.get_by_test_id("training-log-link")).to_be_visible()
    panel = page.locator(".meso-card").filter(has_text="Personal records")
    expect(panel.get_by_text("Bench Press", exact=True)).to_be_visible()
    expect(panel.get_by_text("Back Squat", exact=True)).to_be_visible()
    press(page.get_by_test_id("training-log-link"))
    entries = page.get_by_test_id("training-log-entry")
    expect(entries).to_have_count(2)
    expect(entries.nth(0)).to_contain_text("Coach Casey Coach")
    expect(entries.nth(1)).to_contain_text("Coach Casey Coach")
    assert _fits(page), "the log list scrolls sideways after the coach left"
    shot("after-coach-ended")


@pytest.mark.parametrize("viewport", ["phone"], indirect=True)
def test_offline_navigation_to_the_training_log_lands_on_the_offline_shell(
    browser, live_server, viewport, two_day_plan
):
    p = two_day_plan
    squat = p.squat
    athlete_client = Client()
    athlete_client.force_login(p.athlete)
    wrote = athlete_client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": p.lower.pk}),
        data=json.dumps({"exercise_id": squat.pk, "line": 1, "text": "100 x 5"}),
        content_type="application/json",
    )
    assert wrote.status_code == 200
    context = browser.new_context(
        base_url=live_server.url, service_workers="allow", **viewport["context_args"]
    )
    try:
        client = Client()
        client.force_login(two_day_plan.athlete)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                    "url": live_server.url,
                }
            ]
        )
        page = context.new_page()
        page.goto(reverse("meso:athlete_home"))
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.reload()
        page.wait_for_function(
            "navigator.serviceWorker && navigator.serviceWorker.controller !== null"
        )

        # While online, visit the log and one workout page. The worker must
        # not keep either: a shared phone would replay one athlete's history
        # to the next person.
        page.goto(reverse("meso:athlete_log"))
        entry = page.get_by_test_id("training-log-entry")
        expect(entry).to_have_count(1)
        workout_path = entry.get_attribute("href")
        page.goto(workout_path)
        expect(page.get_by_test_id("workout-set")).to_have_text("Set 1 · 100 kg × 5")

        context.set_offline(True)
        for path in (workout_path, reverse("meso:athlete_log")):
            page.goto(path)
            expect(page.get_by_role("heading", name="You're offline")).to_be_visible()
            expect(page.get_by_text("Lower")).to_have_count(0)
            expect(page.get_by_test_id("workout-set")).to_have_count(0)
            expect(page.get_by_test_id("training-log-entry")).to_have_count(0)
    finally:
        context.close()
