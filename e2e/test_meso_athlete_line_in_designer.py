"""Athlete-authored lines stay identifiable and out of the delivery diff (#599)."""

import re

import pytest
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.meso.models import Prescription
from store_project.meso.models import WeekDelivery
from store_project.meso.serializers import serialize_week_snapshot
from store_project.meso.tests._helpers import sub_line

pytestmark = pytest.mark.django_db

ATHLETE_LINES = ("100 x 5, RPE 8", "105 x 5, RPE 9")
COACH_LINE = "Pause for two seconds"


def test_athlete_lines_are_marked_in_designer_and_omitted_from_delivery_diff(
    page, viewport, press, login, new_page, delivered_plan
):
    coach_line = sub_line(delivered_plan.squat, COACH_LINE, line=3)
    now = timezone.now()
    WeekDelivery.objects.create(
        week=delivered_plan.week,
        delivered_at=now,
        payload=serialize_week_snapshot(delivered_plan.week),
    )

    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    card = page.get_by_test_id("exercise-card").filter(has_text="Box Squat")
    sub_lines = card.get_by_test_id("sub-line-input")

    for index, text in enumerate(ATHLETE_LINES):
        field = sub_lines.nth(index)
        press(field)
        field.fill(text)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and "/cell/" in response.url
            )
        ) as response_info:
            if viewport["is_phone"]:
                sub_lines.nth(index + 1).tap()
            else:
                field.press("Tab")
        assert response_info.value.ok

    athlete_lines = list(
        Prescription.objects.filter(
            exercise_slot=delivered_plan.squat.exercise_slot,
            week=delivered_plan.week,
            athlete_authored=True,
        ).order_by("line")
    )
    assert [line.text for line in athlete_lines] == list(ATHLETE_LINES)

    coach_page = new_page(desktop=True)
    login(delivered_plan.coach, on=coach_page)
    coach_page.goto(reverse("meso:roster"))
    coach_page.locator("a.meso-row").filter(has_text="Alex Athlete").click()
    coach_page.get_by_role("link", name="Open in designer").click()
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()

    for line in athlete_lines:
        mark = coach_page.get_by_test_id(f"cell-line-athlete-{line.pk}")
        expect(mark).to_have_text("athlete")
        expect(mark).to_have_attribute("title", "Logged by your athlete")
    expect(
        coach_page.get_by_test_id(f"cell-line-athlete-{coach_line.pk}")
    ).to_have_count(0)
    expect(
        coach_page.get_by_test_id(f"cell-line-{delivered_plan.squat.pk}-3")
    ).to_have_value(COACH_LINE)

    coach_page.get_by_test_id("deliver-link").click()
    expect(
        coach_page.get_by_role("heading", name=re.compile(r"^Deliver"))
    ).to_be_visible()
    expect(
        coach_page.get_by_text("No changes since you last delivered Wk 1.")
    ).to_be_visible()
    for text in ATHLETE_LINES:
        expect(coach_page.get_by_text(text, exact=True)).to_have_count(0)
