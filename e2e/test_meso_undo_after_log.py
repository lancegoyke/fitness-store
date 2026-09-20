"""#561 in a real browser: a coach undo after "Log session" shows one set.

The Django test (``app/store_project/meso/tests/test_undo_after_log_session.py``)
covers the rule through the views and the presenter's dict, with a payload
built to match what the template consumes. This one answers what that test
can't: whether the athlete's OWN rendered page — the Alpine-hydrated line
input, the structured "Set" row, the warn badge and its CSS class — actually
lands in the fixed state, not just the data behind it. The sequence:

1. the athlete types ``225 x 5`` on Box Squat's first sub-line and blurs it —
   parse-at-commit creates a parsed ``LoggedSet``, ``source_line`` = that cell;
2. the coach rewrites the same sub-line to ``brace harder`` through the real
   designer endpoint (``meso:api_cell_line_write``, a Django test ``Client``
   force-logged-in as the coach — the coach UI isn't the subject of this
   test) — this "reclaims" the line, so the set stops being shown by its own
   line and starts rendering as a structured "Set" row instead;
3. the athlete's page, loaded AFTER the reclaim, shows both the coach's new
   text on the line and the reclaimed Set row's 225/5 at once, and then taps
   "Log session" for real. The client's ``buildPayload`` posts every filled
   Set row, so the set is replaced by a source-less copy carrying
   ``reclaimed_line`` (#541);
4. the coach undoes the rewrite — one undo is enough, since ``cell_line_write``
   snapshots the line's previous text and ``athlete_authored`` flag BEFORE
   writing — so sub-line 1 reads ``225 x 5`` again, this time as a
   coach-owned cell.

Before the fix, the athlete then saw ``225 x 5`` on the line AND the same
225/5 in Set row 1 at once, with the line tinted "not logged as a set" —
``sub_line_warn_reason`` looked only for a row whose ``source_line`` was that
cell, and the copy has none. After the fix the line shows it once, the Set row
is empty, and there is no tint — one ``LoggedSet`` throughout.
"""

import re

import pytest
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet

from e2e.test_meso_reclaim_restore import _blur_first_sub_line
from e2e.test_meso_reclaim_restore import _box_squat_card
from e2e.test_meso_reclaim_restore import _reclaim_sub_line

pytestmark = pytest.mark.django_db


def test_coach_undo_after_log_session_shows_the_set_once(
    page, viewport, shot, press, login, delivered_plan
):
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()

    # --- 1. the athlete types a set and blurs it ---
    card = _box_squat_card(page)
    press(card.get_by_test_id("sub-line-input").first)
    _blur_first_sub_line(page, viewport, card, "225 x 5")

    # --- 2. the coach reclaims that same sub-line ---
    _reclaim_sub_line(delivered_plan)

    # --- 3. the athlete's page, loaded AFTER the reclaim, then "Log session" ---
    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Wait for the hydrated value FIRST — Alpine repopulates `x-model` from the
    # server payload asynchronously, and every other assertion here is only
    # meaningful once that's happened.
    expect(first_line).to_have_value("brace harder")

    # Reachability evidence: the reclaimed set is visible as a structured "Set"
    # row at the same time the coach's text sits on sub-line 1.
    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("225")
    expect(set_row.get_by_placeholder("reps")).to_have_value("5")
    shot("01-reclaimed-both-visible")

    with page.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_response_info:
        press(page.get_by_test_id("session-log"))
    assert log_response_info.value.ok
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("02-logged")

    # --- 4. the coach undoes the rewrite ---
    client = Client()
    client.force_login(delivered_plan.coach)
    response = client.post(
        reverse("meso:api_plan_undo", kwargs={"plan_id": delivered_plan.plan.pk}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    # --- 5. the athlete reloads: the line shows it once, not twice ---
    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Same hydration wait as step 3 — assert this before anything else.
    expect(first_line).to_have_value("225 x 5")

    set_row = card.locator(".meso-set-row").first
    expect(set_row.get_by_placeholder("load")).to_have_value("")
    expect(set_row.get_by_placeholder("reps")).to_have_value("")

    # No warn tint: neither the badge nor the input's warn class survives the
    # undo — the line IS backed by a logged set again (the copy, restored).
    expect(card.get_by_test_id("sub-line-warn").first).not_to_be_visible()
    expect(first_line).not_to_have_class(re.compile("meso-phone-input--warn"))
    shot("03-after-undo")

    rows = list(
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            prescription=delivered_plan.squat,
        ).order_by("pk")
    )
    assert len(rows) == 1, (
        "one performance is logged twice: "
        f"{[(r.pk, r.source_line_id, r.reclaimed_line_id, r.set_number, r.load, r.reps) for r in rows]}"
    )
    assert (rows[0].load, rows[0].reps) == ("225", "5")
