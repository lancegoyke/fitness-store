"""#541 in a real browser: reclaim, "Log session", retype keeps one set.

The Django test (``app/store_project/meso/tests/test_reclaim_restore_after_log.py``)
covers the rule through the views, with a payload built to match what the
presenter renders. This one answers what that test can't: whether an athlete's
own page ever gets into the state the bug needs. It does. The reclaimed row and
the coach's replacement text show on screen at the same time, and a real tap on
"Log session" posts that row. The sequence:

1. the athlete types ``225 x 5`` on Box Squat's first sub-line and blurs it —
   parse-at-commit creates a parsed ``LoggedSet`` A, ``source_line`` = that cell;
2. the coach rewrites the same sub-line to ``brace harder`` through the real
   designer endpoint (``meso:api_cell_line_write``, a Django test ``Client``
   force-logged-in as the coach — the coach UI isn't the subject of this test) —
   this "reclaims" the line, so A stops being shown by its own line and starts
   rendering as a structured "Set" row instead;
3. the athlete's page, loaded AFTER the reclaim, is asserted to show BOTH sub-line
   1's new text and the reclaimed Set row's 225/5 at once — the reachability
   evidence — and then taps "Log session" for real. The client's ``buildPayload``
   (``app/store_project/static/js/meso_athlete.js``) posts every filled Set row,
   so A is replaced by a source-less structured copy S;
4. the athlete types ``225 x 5`` back into sub-line 1 and blurs, which re-links
   S to the line instead of minting a second row.

One performance, so the session ends with one ``LoggedSet`` for Box Squat.
Before the fix it ended with two: S, plus a new parsed row B.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet

pytestmark = pytest.mark.django_db


def _box_squat_card(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Box Squat")


def _reclaim_sub_line(delivered_plan, *, line=1, text="brace harder"):
    """The coach rewrites a sub-line through the real designer endpoint.

    A Django test ``Client`` force-logged-in as the coach — the coach side of
    this sequence isn't what's under test, only whether the athlete's own page
    then shows the consequences.
    """
    client = Client()
    client.force_login(delivered_plan.coach)
    response = client.post(
        reverse(
            "meso:api_cell_line_write",
            kwargs={
                "plan_id": delivered_plan.plan.pk,
                "slot_id": delivered_plan.squat.exercise_slot.pk,
            },
        ),
        data=json.dumps(
            {"week_id": delivered_plan.week.pk, "line": line, "text": text}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _blur_first_sub_line(page, viewport, card, text):
    """Type ``text`` into Box Squat's first sub-line and blur it for real.

    Tab on desktop, tap the next field on phone — a real user's way of moving
    focus off the line, which is what fires the save (mirrors
    ``test_meso_athlete_logging.py``). Awaits the resulting ``/cell/`` POST and
    asserts it succeeded.
    """
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    first_line.fill(text)
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/cell/" in r.url
    ) as cell_response_info:
        if viewport["is_phone"]:
            sub_lines.nth(1).tap()
        else:
            first_line.press("Tab")
    assert cell_response_info.value.ok


def test_reclaim_then_log_then_retype_keeps_one_set(
    page, viewport, shot, press, login, delivered_plan
):
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()

    # --- 1. the athlete types a set and blurs it ---
    card = _box_squat_card(page)
    press(card.get_by_test_id("sub-line-input").first)
    _blur_first_sub_line(page, viewport, card, "225 x 5")

    # --- 2. the coach reclaims that same sub-line ---
    _reclaim_sub_line(delivered_plan)

    # --- 3. the athlete's page, loaded AFTER the reclaim ---
    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Wait for the hydrated value FIRST — Alpine repopulates `x-model` from the
    # server payload asynchronously, and every other assertion here is only
    # meaningful once that's happened.
    expect(first_line).to_have_value("brace harder")

    # Reachability evidence: the reclaimed set is visible as a structured "Set"
    # row at the same time the coach's text sits on sub-line 1.
    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("225")
    expect(set_row.get_by_placeholder("reps")).to_have_value("5")
    shot("01-reclaimed-both-visible")

    with page.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_response_info:
        press(page.get_by_test_id("session-log"))
    assert log_response_info.value.ok
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("02-logged")

    # --- 4. the athlete retypes the same performance onto sub-line 1 ---
    card = _box_squat_card(page)
    press(card.get_by_test_id("sub-line-input").first)
    _blur_first_sub_line(page, viewport, card, "225 x 5")

    rows = list(
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            prescription=delivered_plan.squat,
        ).order_by("pk")
    )
    assert len(rows) == 1, (
        "one performance is logged twice: "
        f"{[(r.pk, r.source_line_id, r.set_number, r.load, r.reps) for r in rows]}"
    )
    assert (rows[0].load, rows[0].reps) == ("225", "5")

    # And the athlete sees it once: on the line they typed it on, with the Set
    # row empty again, not the same 225 x 5 in both places.
    page.reload()
    card = _box_squat_card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("225 x 5")
    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("")
    expect(set_row.get_by_placeholder("reps")).to_have_value("")
    shot("03-after-retype")
