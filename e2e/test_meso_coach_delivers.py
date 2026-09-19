"""Coach + athlete journey: edit a program, deliver it, the athlete sees it (#506, second slice).

The coach's half of this journey always runs at desktop — the designer isn't
built for a phone (#508 shows a fallback under 900px instead) — via a second
browser context from `new_page(desktop=True)`, while the athlete's half runs
at the test's own parametrized viewport. Reaches the designer the way a real
coach would (roster -> athlete row -> profile -> "Open in designer") and the
deliver screen through the designer's own "Deliver" link, so nothing here
`goto`s a coach URL directly.

Since "2d" (docs/meso/decisions.md), delivering a block is NOT a visibility
gate — the athlete already sees every edit live. Delivering only sends a
one-time "your block is ready" email (+ a no-op push here) and stamps
`Week.delivered_at` / writes a `WeekDelivery`. Nothing a user sees reads
`delivered_at` in this one-plan setup, so this test also asserts on the model
directly — that's the only thing that would catch `plan_deliver` silently
skipping the stamp.
"""

import re

import pytest
from django.core import mail
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import WeekDelivery

pytestmark = pytest.mark.django_db

EDITED_SUB_LINE = "Pause two full seconds at the bottom"


def _squat_card(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Back Squat")


def test_coach_edits_and_delivers_the_block(
    page, viewport, shot, press, login, new_page, undelivered_plan
):
    squat_id = undelivered_plan.squat.pk

    # --- Coach: always desktop (the designer has no phone UI, #508) ---
    coach_page = new_page(desktop=True)
    login(undelivered_plan.coach, on=coach_page)

    # Real coach path into the designer: roster -> athlete row -> profile ->
    # "Open in designer". `.click()` throughout the coach half, never
    # `press()` — this context has no touch, whatever the test's own
    # viewport is.
    coach_page.goto(reverse("meso:roster"))
    athlete_row = coach_page.locator("a.meso-row").filter(has_text="Alex Athlete")
    expect(athlete_row).to_be_visible()
    athlete_row.click()

    expect(coach_page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    open_in_designer = coach_page.get_by_role("link", name="Open in designer")
    expect(open_in_designer).to_be_visible()
    open_in_designer.click()

    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()

    # Edit the squat's existing sub-line 1, blur it (Tab — a real desktop
    # user's move-focus gesture), and wait on the write.
    sub_line_input = coach_page.get_by_test_id(f"cell-line-{squat_id}-1")
    sub_line_input.click()
    sub_line_input.fill(EDITED_SUB_LINE)
    with coach_page.expect_response(
        lambda r: r.request.method == "POST" and "/cell/" in r.url
    ) as cell_response_info:
        sub_line_input.press("Tab")
    assert cell_response_info.value.ok

    # Reload and prove the edit survived a real round trip, not just local
    # React state.
    coach_page.reload()
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    reloaded_sub_line = coach_page.get_by_test_id(f"cell-line-{squat_id}-1")
    expect(reloaded_sub_line).to_have_value(EDITED_SUB_LINE)
    shot("01-designer-edited", on=coach_page, viewport_id="desktop")

    # Deliver, via the designer's own "Deliver" link (TopBar.tsx) rather than
    # a goto — it carries `?week=` for the block currently on screen.
    coach_page.get_by_test_id("deliver-link").click()
    expect(
        coach_page.get_by_role("heading", name=re.compile(r"^Deliver"))
    ).to_be_visible()

    with coach_page.expect_response(
        lambda r: r.request.method == "POST" and "/deliver/" in r.url
    ) as deliver_response_info:
        coach_page.get_by_test_id("deliver-send").click()
    assert deliver_response_info.value.ok
    expect(
        coach_page.get_by_role("heading", name="Block delivered to Alex Athlete")
    ).to_be_visible()
    shot("02-delivered", on=coach_page, viewport_id="desktop")

    # Delivery isn't a visibility gate (2d) — nothing the coach or athlete
    # SEES depends on `delivered_at`/`WeekDelivery` in this one-plan setup, so
    # assert on the model directly. This is what catches `plan_deliver`
    # silently skipping the stamp.
    undelivered_plan.week.refresh_from_db()
    assert undelivered_plan.week.delivered_at is not None
    assert WeekDelivery.objects.filter(week=undelivered_plan.week).exists()

    # Reload the deliver screen: it's now a re-delivery of an unchanged week.
    coach_page.reload()
    expect(
        coach_page.get_by_text("No changes since you last delivered Wk 1.")
    ).to_be_visible()

    # --- Athlete: the delivery email is the signal, not `delivered_at` ---
    # The email goes out in `transaction.on_commit`. `live_server` runs on a
    # transactional DB in this process, so the deliver POST really commits
    # and the locmem backend's message lands here (the app tests need
    # `django_capture_on_commit_callbacks` for the same thing).
    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert email.to == [undelivered_plan.athlete.email]
    link_match = re.search(r"https?://\S+/meso/me/", email.body)
    assert link_match, f"no training-home link found in the email body: {email.body!r}"
    home_url = link_match.group(0)

    login(undelivered_plan.athlete)
    page.goto(home_url)

    session_link = page.get_by_role("link", name=re.compile("Lower"))
    expect(session_link).to_be_visible()

    # The multi-week block table (desktop) and the stacked day cards (phone,
    # <640px) are both in the DOM at once — CSS alone picks which shows — so
    # scope to whichever one is actually visible at this viewport.
    if viewport["is_phone"]:
        edited_line = page.get_by_test_id("block-stack-line").filter(
            has_text=EDITED_SUB_LINE
        )
    else:
        edited_line = page.get_by_test_id("block-table").get_by_text(EDITED_SUB_LINE)
    expect(edited_line).to_be_visible()
    shot("03-training-home")

    press(session_link)
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()

    # The session page shows only line 0 as the target. A coach's sub-line is
    # the value of the first "what you did" input, the field the athlete types
    # their own sets into (#524 asks whether it should be). If that changes,
    # this assertion moves with it.
    squat_card = _squat_card(page)
    expect(squat_card.get_by_test_id("sub-line-input").first).to_have_value(
        EDITED_SUB_LINE
    )
    shot("04-session")
