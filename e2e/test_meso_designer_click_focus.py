"""The designer table survives click-type-click-type (issue #597).

Desktop only: the designer isn't editable on a phone (#508 shows a fallback
message instead), so this journey pins `viewport` to a single "desktop"
parametrization rather than running three times over.

Every case here is one shape: an editor is open and dirty, the coach clicks
straight into a DIFFERENT cell, and types. The click has to land — commit
what was open, and leave focus and the caret in the cell that was clicked —
because that is how anyone fills a spreadsheet, and the table advertises
itself as one. Before the fix the first such click only closed the open
editor: the commit's optimistic re-render ran useTableNav's restoration
inside the browser's own focus transfer, and its `.focus()` on the OLD
anchor made Chrome abandon the pending focus, silently landing the next
keystrokes back where the coach had just been.

jsdom cannot see this — it does not model Chrome's "focus changed during
blur, abandon the click's focus" rule — so these are e2e, not vitest.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Prescription

pytestmark = pytest.mark.django_db


def _open_designer(page, live_server, plan):
    page.goto(
        f"{live_server.url}{reverse('meso:designer_plan', kwargs={'plan_id': plan.pk})}"
    )
    expect(page.get_by_test_id("meso-table-view")).to_be_visible()


def _slot(plan, name):
    return ExerciseSlot.objects.get(
        session_slot__mesocycle__plan=plan, name=name, deleted_at__isnull=True
    )


def _cell(plan, name, week_index):
    # line=0 is the prescription itself; lines 1+ are the cell's sub-lines,
    # which are Prescription rows too.
    return Prescription.objects.get(
        exercise_slot=_slot(plan, name), week__index=week_index, line=0
    )


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_naming_a_new_exercise_then_clicking_its_week_cell(
    page, live_server, login, block_plan, shot
):
    """The issue's own repro: name a fresh row, click its Wk 1 cell, type."""
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)

    lower_slot_id = _slot(block_plan.plan, "Back Squat").session_slot_id
    before = set(
        ExerciseSlot.objects.filter(
            session_slot_id=lower_slot_id, deleted_at__isnull=True
        ).values_list("pk", flat=True)
    )
    page.get_by_test_id(f"add-exercise-{lower_slot_id}").click()
    new_slot_id = None
    for _ in range(50):
        now = set(
            ExerciseSlot.objects.filter(
                session_slot_id=lower_slot_id, deleted_at__isnull=True
            ).values_list("pk", flat=True)
        )
        if now - before:
            new_slot_id = (now - before).pop()
            break
        page.wait_for_timeout(100)
    assert new_slot_id is not None, "the new exercise row never appeared"

    name = page.get_by_test_id(f"row-name-{new_slot_id}")
    expect(name).to_be_visible()
    name.click()
    page.keyboard.press("ControlOrMeta+a")  # the row is born named "New exercise"
    page.keyboard.type("Overhead Press")

    wk1 = page.get_by_test_id(
        f"cell-text-{_cell_id_for(block_plan.plan, new_slot_id, 1)}"
    )
    wk1.click()
    # The click has to have moved focus, before a single key is pressed.
    expect(wk1).to_be_focused()
    page.keyboard.type("3x8 @ 95")
    # Commit the LAST editor the way a coach leaving the cell would — the
    # click under test already committed the name. Nothing in the designer
    # saves an editor that is still open and focused.
    page.keyboard.press("Tab")

    expect(name).to_have_value("Overhead Press")
    expect(wk1).to_have_value("3x8 @ 95")
    shot("01-after-typing")

    _reload_and_expect(
        page,
        live_server,
        block_plan.plan,
        {f"row-name-{new_slot_id}": "Overhead Press"},
        {f"cell-text-{_cell_id_for(block_plan.plan, new_slot_id, 1)}": "3x8 @ 95"},
    )


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_typing_in_one_week_cell_then_clicking_the_next(
    page, live_server, login, block_plan
):
    """Cell -> cell, same row: Wk 2 then Wk 3."""
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)

    wk2_id = _cell(block_plan.plan, "Romanian Deadlift", 2).pk
    wk3_id = _cell(block_plan.plan, "Romanian Deadlift", 3).pk

    wk2 = page.get_by_test_id(f"cell-text-{wk2_id}")
    wk3 = page.get_by_test_id(f"cell-text-{wk3_id}")

    wk2.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("3x5 @ 235")
    wk3.click()
    expect(wk3).to_be_focused()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("3x3 @ 250")
    page.keyboard.press("Tab")  # commit the last editor; the click committed Wk 2

    expect(wk2).to_have_value("3x5 @ 235")
    expect(wk3).to_have_value("3x3 @ 250")

    _reload_and_expect(
        page,
        live_server,
        block_plan.plan,
        {},
        {f"cell-text-{wk2_id}": "3x5 @ 235", f"cell-text-{wk3_id}": "3x3 @ 250"},
    )


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_clicking_a_cell_in_another_row(page, live_server, login, block_plan):
    """Cell -> cell across rows, in the same day's table."""
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)

    squat_id = _cell(block_plan.plan, "Back Squat", 1).pk
    rdl_id = _cell(block_plan.plan, "Romanian Deadlift", 1).pk

    squat = page.get_by_test_id(f"cell-text-{squat_id}")
    rdl = page.get_by_test_id(f"cell-text-{rdl_id}")

    squat.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("5x3 @ 85%")
    rdl.click()
    expect(rdl).to_be_focused()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.type("4x6 @ 100")
    page.keyboard.press("Tab")  # commit the last editor; the click committed the squat

    expect(squat).to_have_value("5x3 @ 85%")
    expect(rdl).to_have_value("4x6 @ 100")

    _reload_and_expect(
        page,
        live_server,
        block_plan.plan,
        {},
        {f"cell-text-{squat_id}": "5x3 @ 85%", f"cell-text-{rdl_id}": "4x6 @ 100"},
    )


def _cell_id_for(plan, slot_id, week_index):
    return Prescription.objects.get(
        exercise_slot_id=slot_id, week__index=week_index, line=0
    ).pk


def _reload_and_expect(page, live_server, plan, names, cells):
    """Everything typed is still there after a reload — it really was saved."""
    page.wait_for_timeout(600)  # let the fire-and-forget autosaves land
    _open_designer(page, live_server, plan)
    for testid, value in {**names, **cells}.items():
        expect(page.get_by_test_id(testid)).to_have_value(value)
