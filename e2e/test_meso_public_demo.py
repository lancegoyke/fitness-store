"""The public demo lands in a populated workspace (#650).

An anonymous visitor opens the Meso landing page, clicks "Launch the demo",
and must arrive on a roster that already lists athletes — the landing copy
promises "a populated workspace". The guided tour is still armed, but it
narrates the loaded data: its card never offers to "Add 5 sample athletes",
and the roster never says "No athletes yet".

Runs at every shared viewport (it is a public, phone-reachable entry point).
"""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db


def test_launch_the_demo_lands_on_a_populated_roster(page, shot, press):
    page.goto("/meso/")
    press(page.get_by_role("link", name="Launch the demo").first)

    # The roster lives at /meso/ too (the landing page is its anonymous face),
    # so wait on the roster's own content rather than a URL change.
    expect(page.get_by_text("Maya").first).to_be_visible()
    expect(page.get_by_text("No athletes yet")).to_have_count(0)

    card = page.locator(".meso-tour-card")
    expect(card).to_be_visible()
    expect(card.get_by_text("Add 5 sample athletes")).to_have_count(0)
    expect(card.get_by_role("button", name="Add 5 sample athletes")).to_have_count(0)
    shot("roster-with-tour")
