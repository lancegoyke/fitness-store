"""Exercise-name suggestions in the designer's row-name input (#608).

Desktop only, like the other designer journeys. The popup is portaled and
position: fixed, and Chrome's real focus/keyboard behaviour decides whether
ArrowDown+Enter stays in the input — none of which jsdom can see.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.exercises.models import Exercise
from store_project.meso.models import ExerciseSlot

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


def _retype(page, slot, text):
    """Replace the row name with `text`, one keystroke at a time."""
    name = page.get_by_test_id(f"row-name-{slot.pk}")
    name.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type(text)
    return name


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_pick_a_catalog_suggestion_renames_and_links(
    page, live_server, login, block_plan
):
    goblet = Exercise.objects.create(name="Goblet Squat", slug="goblet-squat")
    login(block_plan.coach)
    slot = _slot(block_plan.plan, "Romanian Deadlift")
    _open_designer(page, live_server, block_plan.plan)

    name = _retype(page, slot, "sq")
    # The coach's own "Back Squat" is listed (hinted "yours") ahead of the catalog.
    listbox = page.get_by_role("listbox")
    expect(listbox).to_be_visible()
    options = listbox.get_by_role("option")
    expect(options).to_have_count(2)
    expect(options.nth(0)).to_contain_text("Back Squat")
    expect(options.nth(1)).to_contain_text("Goblet Squat")

    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(name).to_have_value("Goblet Squat")
    expect(name).to_be_focused()
    expect(listbox).to_have_count(0)
    # Let the POST land before the DB check / reload.
    page.wait_for_load_state("networkidle")

    slot.refresh_from_db()
    assert slot.name == "Goblet Squat"
    assert slot.exercise_id == goblet.pk

    page.reload()
    expect(page.get_by_test_id(f"row-name-{slot.pk}")).to_have_value("Goblet Squat")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_typing_a_different_name_after_a_pick_unlinks(
    page, live_server, login, block_plan
):
    goblet = Exercise.objects.create(name="Goblet Squat", slug="goblet-squat")
    login(block_plan.coach)
    slot = _slot(block_plan.plan, "Romanian Deadlift")
    _open_designer(page, live_server, block_plan.plan)

    _retype(page, slot, "gob")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")
    slot.refresh_from_db()
    assert slot.exercise_id == goblet.pk

    # A free-text name that is not a pick unlinks (free text always wins).
    name = _retype(page, slot, "Goblet Squat (pause)")
    expect(page.get_by_role("listbox")).to_have_count(0)  # nothing left to suggest
    expect(name).to_have_value("Goblet Squat (pause)")
    page.keyboard.press("Tab")
    page.wait_for_load_state("networkidle")

    slot.refresh_from_db()
    assert slot.name == "Goblet Squat (pause)"
    assert slot.exercise_id is None
