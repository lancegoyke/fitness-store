"""The designer athlete preview matches each real session in a multi-week block."""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Session

pytestmark = pytest.mark.django_db


def _preview_cards(page):
    return page.get_by_test_id("athlete-preview-exercise-card").evaluate_all(
        r"""
        cards => cards.map(card => ({
          name: card.querySelector('.meso-phone-exercise-name').textContent.trim(),
          target: card.querySelector('.meso-phone-exercise-target').textContent.replace(/\s+/g, ' ').trim(),
          lines: [...card.querySelectorAll('.meso-phone-exercise-line')]
            .map(input => input.value)
            .filter(value => value.trim()),
        }))
        """
    )


def _athlete_cards(page, count):
    cards = page.get_by_test_id("exercise-card")
    expect(cards).to_have_count(count)
    return cards.evaluate_all(
        r"""
        cards => cards.map(card => ({
          name: card.querySelector(':scope > div:first-child > div:first-child').textContent.trim(),
          target: card.querySelector(':scope > div:first-child .meso-row-meta.meso-mono')
            .textContent.replace(/\s+/g, ' ').trim(),
          lines: [...card.querySelectorAll('[data-testid="sub-line-input"]')]
            .map(input => input.value)
            .filter(value => value.trim()),
        }))
        """
    )


def test_designer_athlete_preview_matches_every_real_session(
    page, login, new_page, block_plan
):
    back_squat = ExerciseSlot.objects.get(
        session_slot__mesocycle=block_plan.weeks[0].mesocycle,
        name="Back Squat",
    )
    back_squat.note = "Keep your knees tracking over your toes"
    back_squat.save(update_fields=["note"])

    sessions = list(
        Session.objects.filter(week__in=block_plan.weeks, deleted_at__isnull=True)
        .select_related("week", "session_slot")
        .order_by("week__index", "session_slot__order", "session_slot__day_number")
    )

    coach_page = new_page(desktop=True)
    login(block_plan.coach, on=coach_page)
    coach_page.goto(
        reverse("meso:designer_plan", kwargs={"plan_id": block_plan.plan.pk})
    )
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    coach_page.get_by_role("tab", name="Athlete view").click()

    preview = coach_page.locator(".meso-athlete-preview")
    preview_text = preview.inner_text()
    assert not re.search(r"\bwed\b", preview_text, re.IGNORECASE)
    assert "knee-safe" not in preview_text.lower()
    assert "box squat" not in preview_text.lower()

    login(block_plan.athlete)
    for session in sessions:
        week_button = coach_page.get_by_test_id(
            f"athlete-preview-week-{session.week_id}"
        )
        week_button.click()
        expect(week_button).to_have_attribute("aria-selected", "true")

        day_button = coach_page.get_by_test_id(
            f"athlete-preview-day-{session.day_number}"
        )
        day_button.click()
        expect(day_button).to_have_attribute("aria-selected", "true")
        expected = _preview_cards(coach_page)

        page.goto(reverse("meso:athlete_session", kwargs={"pk": session.pk}))
        actual = _athlete_cards(page, len(expected))
        assert actual == expected
