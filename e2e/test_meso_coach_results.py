"""Coach journey: the athlete's logged set shows up on the results page (#506).

The log is produced through the exact endpoints the athlete's own UI calls —
POST the cell, then POST the log exactly as "Log session" sends it (`{status:
"done", sets: []}`, no structured set rows filled) — via the Django test
client logged in as the athlete, not by driving the athlete's browser a
second time (`test_meso_athlete_logging.py` already covers that journey).
Real server code produces the data either way; this keeps the coach
journey's own assertions independent of whatever the athlete's browser
session does.
"""

import json
import re

import pytest
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db


def _log_a_box_squat_set(delivered_plan):
    client = Client()
    client.force_login(delivered_plan.athlete)
    cell_response = client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": delivered_plan.session.pk}),
        data=json.dumps(
            {"exercise_id": delivered_plan.squat.pk, "line": 1, "text": "100 x 5"}
        ),
        content_type="application/json",
    )
    assert cell_response.status_code == 200
    log_response = client.post(
        reverse("meso:athlete_log_session", kwargs={"pk": delivered_plan.session.pk}),
        data=json.dumps({"status": "done", "sets": []}),
        content_type="application/json",
    )
    assert log_response.status_code == 200


def test_coach_sees_the_logged_set(page, viewport, shot, press, login, delivered_plan):
    _log_a_box_squat_set(delivered_plan)

    login(delivered_plan.coach)
    page.goto(reverse("meso:roster"))

    # Scoped to the roster row itself (`a.meso-row`) — the athlete's name also
    # appears a second time, as a plain link, in the "Recent activity" feed
    # (the set we just logged shows up there too), which a bare role/name
    # match would collide with.
    athlete_row = page.locator("a.meso-row").filter(has_text="Alex Athlete")
    expect(athlete_row).to_be_visible()
    press(athlete_row)

    expect(page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    latest_session_card = page.get_by_role("link").filter(has_text="Latest session")
    expect(latest_session_card).to_be_visible()
    press(latest_session_card)

    expect(page).to_have_url(re.compile(r"/meso/results/\d+/$"))
    expect(page.get_by_text("Logged session")).to_be_visible()

    row = page.get_by_test_id("results-row").filter(has_text="Box Squat")
    expect(row.get_by_test_id("results-logged")).to_have_text("1×5 @ 100 kg")
    shot("01-results")
