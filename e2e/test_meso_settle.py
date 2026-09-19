"""Settle journey: a quietly-abandoned typed log settles to Logged (5b, #506 third slice).

Sets up a PENDING log the way the athlete's own UI creates one — a typed
sub-line cell write (`meso:athlete_cell_write`), "Log session" never
pressed — then calls `settle.settle_log` exactly as the hourly sweep
(`settle_quiet_logs`, settle.py ~:210) does, and checks that both the
athlete's own page and the coach's results page read the settled log exactly
like a tapped one.
"""

import json
import re
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from playwright.sync_api import expect
from store_project.meso import settle
from store_project.meso.models import SessionLog

pytestmark = pytest.mark.django_db


def test_a_settled_log_reads_as_logged(
    page, viewport, shot, press, login, new_page, delivered_plan
):
    # --- setup: type a set the way the athlete's UI does, never tap "Log session" ---
    setup_client = Client()
    setup_client.force_login(delivered_plan.athlete)
    cell_response = setup_client.post(
        reverse("meso:athlete_cell_write", kwargs={"pk": delivered_plan.session.pk}),
        data=json.dumps(
            {"exercise_id": delivered_plan.squat.pk, "line": 1, "text": "100 x 5"}
        ),
        content_type="application/json",
    )
    assert cell_response.status_code == 200

    log = (
        SessionLog.objects.filter(
            session=delivered_plan.session, athlete=delivered_plan.athlete
        )
        .order_by("-created_at")
        .first()
    )
    assert log is not None
    assert log.status == SessionLog.Status.PENDING

    # Prove the athlete's page reads "To do" before settling — so the rest of
    # this test proves settle is what flips it, not something else.
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_test_id("session-status")).to_have_text("To do")

    # Backdate past the quiet period and settle exactly as the sweep does.
    cutoff = timezone.now() - settle.quiet_period()
    SessionLog.objects.filter(pk=log.pk).update(
        last_activity_at=cutoff - timedelta(hours=1)
    )
    assert settle.settle_log(log.pk, cutoff=cutoff) is True

    # --- athlete: reload, badge flips, the typed line is still there ---
    page.reload()
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    card = page.get_by_test_id("exercise-card").filter(has_text="Box Squat")
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("100 x 5")
    shot("01-athlete")

    # --- coach: roster -> athlete row -> "Latest session" -> results ---
    coach_page = new_page()
    login(delivered_plan.coach, on=coach_page)
    coach_page.goto(reverse("meso:roster"))

    athlete_row = coach_page.locator("a.meso-row").filter(has_text="Alex Athlete")
    expect(athlete_row).to_be_visible()
    press(athlete_row)

    expect(coach_page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    latest_session_card = coach_page.get_by_role("link").filter(
        has_text="Latest session"
    )
    expect(latest_session_card).to_be_visible()
    press(latest_session_card)

    expect(coach_page).to_have_url(re.compile(r"/meso/results/\d+/$"))
    expect(coach_page.get_by_text("Logged session")).to_be_visible()
    row = coach_page.get_by_test_id("results-row").filter(has_text="Box Squat")
    expect(row.get_by_test_id("results-logged")).to_have_text("1×5 @ 100 kg")
    shot("02-coach", on=coach_page)
