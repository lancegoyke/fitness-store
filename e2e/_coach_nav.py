"""Shared coach-side navigation for the athlete-logging journeys.

The coach is always a desktop surface (see `new_page(desktop=True)`), so it
clicks. It walks roster -> athlete -> "Latest session" -> results, the way
`test_meso_coach_results.py` does.
"""

import re

from django.urls import reverse
from playwright.sync_api import expect


def open_latest_results(coach_page):
    """Open the athlete's latest session results from the coach roster."""
    coach_page.goto(reverse("meso:roster"))
    athlete_row = coach_page.locator("a.meso-row").filter(has_text="Alex Athlete")
    expect(athlete_row).to_be_visible()
    athlete_row.click()
    expect(coach_page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    latest = coach_page.get_by_role("link").filter(has_text="Latest session")
    expect(latest).to_be_visible()
    latest.click()
    expect(coach_page).to_have_url(re.compile(r"/meso/results/\d+/$"))
    expect(coach_page.get_by_text("Logged session")).to_be_visible()


def completion_tile(coach_page):
    """The first stat tile on the results page: "NN%" over the sets label."""
    return coach_page.locator(".meso-stats .meso-card").first


def assert_completion(coach_page, percent, label):
    tile = completion_tile(coach_page)
    expect(tile).to_be_visible()
    text = tile.inner_text()
    assert text.split()[0] == f"{percent}%", text
    assert label in " ".join(text.split()), text
