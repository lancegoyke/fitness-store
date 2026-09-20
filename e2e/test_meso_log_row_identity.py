"""#567 B in a real browser: a second identical set typed into the empty row.

The Django test (``app/store_project/meso/tests/test_log_row_identity.py``)
covers the rule through the views with a payload written by hand. This one
answers what that test can't: whether an athlete's own page ever offers them
the row this bug needs, and whether a real tap on "Log session" posts it.

It does, and the trap is that the row looks empty. The sequence:

1. the athlete types ``225 x 5`` on Box Squat's first sub-line and blurs it —
   parse-at-commit creates a ``LoggedSet`` whose ``set_number`` is the
   sub-line's own position, 1, and which is HIDDEN from the structured logger
   because the line's text is already showing that performance;
2. so Set row 1 renders empty, which is exactly where an athlete would log
   their next set. The test asserts that state before going on — it is the
   reachability evidence;
3. the athlete does a second set of the same weight for the same reps, types
   ``225``/``5`` into that empty-looking Set row and taps "Log session".

Before the fix the server matched the posted row against the hidden one by
``(prescription, set_number, values)``, read it as a restatement of a row the
page never showed, and dropped it: nothing was created, the response echoed no
sets, and the row un-ticked itself. The athlete's second set was gone, and
retyping it could never persist. With row identity in the payload the posted
row carries a client-minted id instead of a set number the client can't vouch
for, so it is a new performance and both sets survive a reload.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet

pytestmark = pytest.mark.django_db


def _box_squat_card(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Box Squat")


def _blur_first_sub_line(page, viewport, card, text):
    """Type ``text`` into Box Squat's first sub-line and blur it for real.

    Tab on desktop, tap the next field on phone — a real user's way of moving
    focus off the line, which is what fires the save (mirrors
    ``test_meso_reclaim_restore.py``). Awaits the resulting ``/cell/`` POST.
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


def _squat_rows(delivered_plan):
    return list(
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            prescription=delivered_plan.squat,
        ).order_by("pk")
    )


def test_a_second_identical_set_in_the_empty_row_survives_a_reload(
    page, viewport, shot, press, login, delivered_plan
):
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()

    # --- 1. the athlete types their first set onto the tracking line ---
    card = _box_squat_card(page)
    press(card.get_by_test_id("sub-line-input").first)
    _blur_first_sub_line(page, viewport, card, "225 x 5")

    # --- 2. the trap: that set is hidden, so Set row 1 renders EMPTY ---
    # Reload rather than trusting the live page, because it is the SERVER's
    # view of the row that has to offer the empty row for this to be reachable.
    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Wait for the hydrated value FIRST — Alpine repopulates `x-model` from the
    # server payload asynchronously, and every assertion below is only
    # meaningful once that has happened.
    expect(first_line).to_have_value("225 x 5")
    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("")
    expect(set_row.get_by_placeholder("reps")).to_have_value("")
    shot("01-hidden-row-leaves-set-1-empty")

    # --- 3. a second set of the same weight and reps, typed into that row ---
    set_row.get_by_placeholder("load").fill("225")
    set_row.get_by_placeholder("reps").fill("5")
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_response_info:
        press(page.get_by_test_id("session-log"))
    assert log_response_info.value.ok
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("02-logged")

    rows = _squat_rows(delivered_plan)
    assert len(rows) == 2, (
        "the athlete's second set was swallowed by the hidden row: "
        f"{[(r.pk, r.source_line_id, r.set_number, r.load, r.reps) for r in rows]}"
    )
    assert sorted((r.load, r.reps) for r in rows) == [("225", "5"), ("225", "5")]

    # --- 4. and after a reload both are on the page, once each ---
    page.reload()
    card = _box_squat_card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("225 x 5")
    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("225")
    expect(set_row.get_by_placeholder("reps")).to_have_value("5")
    # The hidden row was renumbered off set 1 by the save, so it now sits on a
    # later number — still hidden, so that row must render empty rather than
    # showing the same performance a third time.
    second_row = card.locator(".meso-set-row").nth(1)
    expect(second_row.get_by_placeholder("load")).to_have_value("")
    expect(second_row.get_by_placeholder("reps")).to_have_value("")
    shot("03-both-sets-after-reload")
