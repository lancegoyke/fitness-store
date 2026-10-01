"""Escape in the designer table is "cancel" (#653), and a new row types clean (#636).

Desktop only, like the other designer journeys. jsdom can't see Chrome's real
focus behaviour, so the focus assertions live here.
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


def _active_tag(page):
    return page.evaluate("document.activeElement && document.activeElement.tagName")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_escape_cancels_the_edit_and_closes_the_editor(
    page, live_server, login, block_plan
):
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)
    slot = ExerciseSlot.objects.get(
        session_slot__mesocycle__plan=block_plan.plan,
        name="Back Squat",
        deleted_at__isnull=True,
    )
    cell = Prescription.objects.get(exercise_slot=slot, week__index=1, line=0)
    original = cell.text

    editor = page.get_by_test_id(f"cell-text-{cell.pk}")
    editor.click()
    page.keyboard.type("9x9 @ 999")
    page.keyboard.press("Escape")

    # Previous value back, editor closed: focus is on the cell, not an input.
    expect(editor).to_have_value(original)
    assert _active_tag(page) == "TD"
    # A second Escape leaves the table.
    page.keyboard.press("Escape")
    assert _active_tag(page) == "BODY"
    # Nothing was saved.
    cell.refresh_from_db()
    assert cell.text == original


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_tab_after_escape_moves_to_the_next_cell(page, live_server, login, block_plan):
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)
    slot = ExerciseSlot.objects.get(
        session_slot__mesocycle__plan=block_plan.plan,
        name="Back Squat",
        deleted_at__isnull=True,
    )
    week1 = Prescription.objects.get(exercise_slot=slot, week__index=1, line=0)
    week2 = Prescription.objects.get(exercise_slot=slot, week__index=2, line=0)

    page.get_by_test_id(f"cell-text-{week1.pk}").click()
    page.keyboard.type("9x9 @ 999")
    page.keyboard.press("Escape")
    assert _active_tag(page) == "TD"

    # #656: Tab leaves the cancelled cell for the next one (it used to re-enter
    # the same editor), and Shift+Tab comes back.
    page.keyboard.press("Tab")
    expect(page.get_by_test_id(f"cell-text-{week2.pk}")).to_be_focused()
    page.keyboard.press("Escape")
    page.keyboard.press("Shift+Tab")
    expect(page.get_by_test_id(f"cell-text-{week1.pk}")).to_be_focused()


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_tab_from_a_skipped_cell_td_moves_to_the_next_editable_cell(
    page, live_server, login, block_plan
):
    """#664: Tab from a skipped cell's <td> reaches the next editable cell.

    A skipped cell has no editor; Tab used to fall into its Unskip button.
    """
    slot = ExerciseSlot.objects.get(
        session_slot__mesocycle__plan=block_plan.plan,
        name="Back Squat",
        deleted_at__isnull=True,
    )
    week1 = Prescription.objects.get(exercise_slot=slot, week__index=1, line=0)
    week2 = Prescription.objects.get(exercise_slot=slot, week__index=2, line=0)
    Prescription.objects.filter(pk=week1.pk).update(skipped=True)
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)

    td = page.get_by_test_id(f"cell-{slot.pk}-{week1.week_id}")
    td.click(position={"x": 4, "y": 4})
    assert _active_tag(page) == "TD"
    page.keyboard.press("Tab")
    expect(page.get_by_test_id(f"cell-text-{week2.pk}")).to_be_focused()
    page.screenshot(path="e2e/screenshots/tab_from_a_skipped_cell_td.png")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_tab_after_escape_off_the_table_edge_leaves_the_grid(
    page, live_server, login, block_plan
):
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)
    rests = page.locator("[data-testid^='row-rest-']")
    last = rests.nth(rests.count() - 1)
    # Only the final row of the final day has no landable stop after Rest if
    # no later table follows; if one does, the Tab simply moves there. Either
    # way focus must not land back in the cancelled Rest editor.
    last.click()
    page.keyboard.press("Escape")
    assert _active_tag(page) == "TD"
    page.keyboard.press("Tab")
    expect(last).not_to_be_focused()


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_add_exercise_then_type_gives_exactly_the_typed_name(
    page, live_server, login, block_plan
):
    login(block_plan.coach)
    _open_designer(page, live_server, block_plan.plan)
    lower = ExerciseSlot.objects.get(
        session_slot__mesocycle__plan=block_plan.plan,
        name="Back Squat",
        deleted_at__isnull=True,
    ).session_slot_id

    page.get_by_test_id(f"add-exercise-{lower}").click()
    focused = page.locator(":focus")
    expect(focused).to_have_attribute("placeholder", "New exercise")
    page.keyboard.type("Romanian Deadlift")
    page.keyboard.press("Tab")

    names = set()
    for _ in range(50):
        names = set(
            ExerciseSlot.objects.filter(
                session_slot_id=lower, deleted_at__isnull=True
            ).values_list("name", flat=True)
        )
        if "Romanian Deadlift" in names:
            break
        page.wait_for_timeout(100)
    assert "Romanian Deadlift" in names
    assert not any("New exerciseRomanian" in n for n in names)
