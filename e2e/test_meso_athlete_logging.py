"""Athlete journey: type a set, reload, Log session (issue #506, first slice).

Cookie-logs in as the athlete (the real login form is `test_login.py`'s job)
and drives the delivered "Lower" session the way an athlete would: focus a
sub-line, type a performed set, blur it (parse-at-commit — 5a — turns it into
a silent `LoggedSet`), reload to prove the line and its parsed set survived,
then "Log session" and prove the badge sticks through another reload.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription

pytestmark = pytest.mark.django_db


def _box_squat_card(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Box Squat")


def test_athlete_logs_a_typed_set(page, viewport, shot, press, login, delivered_plan):
    login(delivered_plan.athlete)

    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    shot("01-open")

    card = _box_squat_card(page)
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    press(first_line)
    first_line.fill("100 x 5")

    # Move focus off the line the way a real user would — Tab on desktop,
    # tapping the next field on phone — which blurs it and fires the save.
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/cell/" in r.url
    ) as cell_response_info:
        if viewport["is_phone"]:
            sub_lines.nth(1).tap()
        else:
            first_line.press("Tab")
    assert cell_response_info.value.ok

    # First-ever Box Squat set for this athlete — a new best. If the server
    # doesn't say so, that's a real finding, not something to paper over by
    # loosening this assertion.
    expect(card.get_by_test_id("sub-line-pr").first).to_be_visible()
    shot("02-typed")

    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Wait for the hydrated value FIRST — Alpine repopulates `x-model` from
    # the server payload asynchronously, and every other assertion on this
    # card is only meaningful once that's happened.
    expect(first_line).to_have_value("100 x 5")
    first_row = first_line.locator("xpath=..")
    expect(first_row.get_by_test_id("sub-line-warn")).not_to_be_visible()
    shot("03-reloaded")

    # Exactly one parsed LoggedSet backs this sub-line, with the load/reps
    # the athlete typed.
    source_line = Prescription.objects.get(
        exercise_slot=delivered_plan.squat.exercise_slot,
        week=delivered_plan.week,
        line=1,
    )
    logged_sets = list(LoggedSet.objects.filter(source_line=source_line))
    assert len(logged_sets) == 1
    assert logged_sets[0].load == "100"
    assert logged_sets[0].reps == "5"

    with page.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_response_info:
        press(page.get_by_test_id("session-log"))
    assert log_response_info.value.ok
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("04-logged")

    page.reload()
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    card = _box_squat_card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("100 x 5")
