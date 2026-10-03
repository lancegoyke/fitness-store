"""A swap after logging says which lift the sets were performed as (#714).

The athlete logs "100 x 5" on week-1 Box Squat and finishes; the coach then
renames the row to Front Squat. Every surface that shows the new name must
also show a quiet "logged as Box Squat" hint: the coach's results page, the
designer cell, and the athlete's own session card. The two live surfaces are
then pushed through a real sync poll (a second rename by the coach) to prove
the hint comes from each poll's server payload and survives the merge.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db

HINT = "logged as Box Squat"


def _rename(plan, prescription, name):
    coach_client = Client()
    coach_client.force_login(plan.coach)
    response = coach_client.post(
        reverse(
            "meso:api_prescription_patch",
            kwargs={"plan_id": plan.plan.pk, "pk": prescription.pk},
        ),
        data=json.dumps({"name": name}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return coach_client


def test_logged_as_hint_after_a_swap(
    page, viewport, shot, login, new_page, logged_plan
):
    p = logged_plan
    _rename(p, p.squat, "Front Squat")

    # --- athlete's own week-1 session card ---
    login(p.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": p.session.pk}))
    card = page.get_by_test_id("exercise-card").filter(has_text="Front Squat")
    expect(card).to_have_count(1)
    athlete_hint = card.get_by_test_id("logged-as-hint")
    expect(athlete_hint).to_have_text(HINT)
    expect(athlete_hint).to_be_visible()
    shot("01-athlete-card")

    # --- coach results page (at the test's viewport) ---
    results_page = new_page()
    login(p.coach, on=results_page)
    results_page.goto(
        reverse("meso:results_session", kwargs={"session_id": p.session.pk})
    )
    row = results_page.get_by_test_id("results-row").filter(has_text="Front Squat")
    expect(row).to_have_count(1)
    results_hint = row.get_by_test_id("logged-as-hint")
    expect(results_hint).to_have_text(HINT)
    # One line: the hint is no taller than a line of its own text.
    box = results_hint.bounding_box()
    assert box["height"] < 20, box
    if viewport["is_phone"]:
        width = results_page.evaluate("document.documentElement.scrollWidth")
        assert width <= viewport["context_args"]["viewport"]["width"], width
    shot("02-results", on=results_page)

    # --- designer cell (a desktop surface) ---
    coach_page = new_page(desktop=True)
    login(p.coach, on=coach_page)
    coach_page.goto(reverse("meso:designer_plan", kwargs={"plan_id": p.plan.pk}))
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    cell_hint = coach_page.get_by_test_id(f"logged-as-hint-{p.squat.pk}")
    expect(cell_hint).to_have_text(HINT)
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{p.squat.pk}")
    expect(marker).to_be_visible()
    shot("03-designer", on=coach_page, viewport_id="desktop")

    # --- live sync: a second rename makes both live pages merge a new payload ---
    _rename(p, p.squat, "Pause Front Squat")
    expect(
        page.get_by_test_id("exercise-card").filter(has_text="Pause Front Squat")
    ).to_have_count(1, timeout=15_000)
    expect(
        page.get_by_test_id("exercise-card")
        .filter(has_text="Pause Front Squat")
        .get_by_test_id("logged-as-hint")
    ).to_have_text(HINT)
    expect(
        coach_page.get_by_test_id(f"row-name-{p.squat.exercise_slot_id}")
    ).to_have_value("Pause Front Squat", timeout=15_000)
    expect(coach_page.get_by_test_id(f"logged-as-hint-{p.squat.pk}")).to_have_text(HINT)
