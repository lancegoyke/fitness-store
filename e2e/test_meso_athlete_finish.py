"""Athlete journey: type the sets, close and reopen, Finish session (#578 stage 4).

The athlete logs by typing lines under "what you did" and nothing else. The
header reads "N of M sets logged" (M = the prescribed sets, the same count the
coach's results use), "Finish session" is the one button, and the coach sees
the same count on the results page.

Drives the delivered "Lower" session (Box Squat 3 sets + RDL 3 sets, so M = 6)
through real navigation from the athlete home, blurring each line the way a
user does (Tab on desktop, tapping the next field on phone) and waiting for
each `/cell/` response before the next blur — `live_server` on in-memory
SQLite collides on two concurrent writes.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog

from e2e._coach_nav import assert_completion
from e2e._coach_nav import open_latest_results

pytestmark = pytest.mark.django_db

SETS = ["70 x 6", "70 x 6", "70 x 5"]
HEADER_0 = "0 of 6 sets logged"
HEADER_3 = "3 of 6 sets logged"


def _open_session_from_home(page, press):
    """Athlete home -> tap the Lower session, as a real athlete does."""
    page.goto(reverse("meso:athlete_home"))
    press(page.get_by_role("link", name=re.compile(r"Lower")).first)
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()


def _squat_lines(page):
    card = page.get_by_test_id("exercise-card").filter(has_text="Box Squat")
    return card.get_by_test_id("sub-line-input")


def _header(page):
    return page.get_by_test_id("set-progress")


def _next_field(page, lines, i, count):
    """The field a phone user taps to blur line ``i`` of ``count``.

    The squat has exactly ``count`` lines, so the last one blurs by tapping
    into the RDL's first line instead.
    """
    if i + 1 < count:
        return lines.nth(i + 1)
    return (
        page.get_by_test_id("exercise-card")
        .filter(has_text="RDL")
        .get_by_test_id("sub-line-input")
        .first
    )


def test_athlete_types_three_sets_and_finishes(
    page, context, viewport, shot, press, login, new_page, delivered_plan
):
    login(delivered_plan.athlete)
    _open_session_from_home(page, press)
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    expect(_header(page)).to_have_text(HEADER_0)
    assert _header(page).inner_text().strip() == HEADER_0
    expect(page.locator(".meso-set-row")).to_have_count(0)
    expect(page.get_by_test_id("session-save")).to_have_count(0)
    expect(page.get_by_text("Save progress")).to_have_count(0)
    expect(page.get_by_test_id("log-instruction")).to_be_visible()
    expect(page.get_by_test_id("session-finish")).to_be_visible()
    shot("01-opened")

    lines = _squat_lines(page)
    for i, text in enumerate(SETS):
        line = lines.nth(i)
        press(line)
        line.fill(text)
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/cell/" in r.url
        ) as response_info:
            if viewport["is_phone"]:
                _next_field(page, lines, i, len(SETS)).tap()
            else:
                line.press("Tab")
        assert response_info.value.ok
    expect(_header(page)).to_have_text(HEADER_3)
    assert _header(page).inner_text().strip() == HEADER_3
    shot("02-three-typed")

    # "Closes and reopens": a brand-new page in the same context, same route.
    page.close()
    reopened = context.new_page()
    _open_session_from_home(reopened, press)
    reopened_lines = _squat_lines(reopened)
    for i, text in enumerate(SETS):
        expect(reopened_lines.nth(i)).to_have_value(text)
    expect(reopened.get_by_test_id("set-progress")).to_have_text(HEADER_3)
    expect(reopened.get_by_test_id("session-status")).to_have_text("To do")
    shot("03-reopened", on=reopened)

    # Finish session.
    with reopened.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_info:
        press(reopened.get_by_test_id("session-finish"))
    assert log_info.value.ok
    expect(reopened.get_by_test_id("session-status")).to_have_text("Logged")
    expect(reopened.get_by_test_id("session-finish")).to_be_hidden()
    expect(reopened.get_by_test_id("log-instruction")).to_be_hidden()
    expect(reopened.get_by_test_id("set-progress")).to_have_text(HEADER_3)
    shot("04-finished", on=reopened)

    reopened.reload()
    expect(reopened.get_by_test_id("session-status")).to_have_text("Logged")
    expect(reopened.get_by_test_id("session-finish")).to_be_hidden()
    expect(reopened.get_by_test_id("set-progress")).to_have_text(HEADER_3)

    # DB: three typed sets, each anchored to its line, and the log is done.
    log = SessionLog.objects.get(
        session=delivered_plan.session, athlete=delivered_plan.athlete
    )
    assert log.status == SessionLog.Status.DONE
    sets = list(LoggedSet.objects.filter(session_log=log))
    assert len(sets) == 3, sets
    assert all(s.source_line_id is not None for s in sets), sets

    # Coach: roster -> athlete -> latest session -> results.
    coach_page = new_page(desktop=True)
    login(delivered_plan.coach, on=coach_page)
    open_latest_results(coach_page)
    assert_completion(coach_page, 50, HEADER_3)
    shot("05-coach-results", on=coach_page, viewport_id="desktop")
