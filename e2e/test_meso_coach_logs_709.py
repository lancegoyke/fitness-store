"""A coach logs the athlete's sets from the designer grid (#709, PR 1).

On a session the athlete has STARTED, a coach's new sub-line that reads as one
set (`5 @ 225`) is a "coach set line": it counts as the athlete's logged set,
shows a "logged" chip in the designer and "logged by coach" on the athlete's
page. Neither side overwrites the other: a NEW line that collides moves to the
next free line, and a coach's EDIT of a line the athlete has since claimed is
refused (visibly). There is no live sync yet, so each side sees the other's
lines only after a reload.

The athlete half runs at every viewport; the coach is always a desktop
context. Journeys 1 and 3 also run the athlete on WebKit (the coach stays on
Chromium).
"""

import re
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription

pytestmark = pytest.mark.django_db

ENGINES = ["chromium", "webkit"]


def _is_cell_post(response):
    return response.request.method == "POST" and "/cell/" in response.url


@pytest.fixture
def engine():
    """The athlete's browser engine; a test opts into WebKit by parametrizing it."""
    return "chromium"


@pytest.fixture
def athlete(request, engine, viewport, playwright, page, login, live_server):
    """The athlete's page and a `login(user)` for it, on `engine`.

    Chromium reuses the test's own `page`/`login`. WebKit launches its own
    browser with the viewport's context args and cookie-logs in the same way
    the `login` fixture does; everything it launches is closed afterwards.
    """
    if engine == "chromium":
        yield SimpleNamespace(page=page, login=login, engine=engine)
        return
    browser = playwright.webkit.launch()
    context = browser.new_context(
        **viewport["context_args"],
        base_url=live_server.url,
        service_workers="block",
    )
    webkit_page = context.new_page()

    def _login(user):
        client = Client()
        client.force_login(user)
        cookie = client.cookies[settings.SESSION_COOKIE_NAME]
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": cookie.value,
                    "url": live_server.url,
                }
            ]
        )

    try:
        yield SimpleNamespace(page=webkit_page, login=_login, engine=engine)
    finally:
        context.close()
        browser.close()


@pytest.fixture
def snap(shot, viewport, athlete):
    """`snap(step, page, desktop=False)` — `shot` named for viewport + engine."""

    def _snap(step, target, desktop=False):
        vid = "desktop" if desktop else viewport["id"]
        if athlete.engine != "chromium" and not desktop:
            vid = f"{vid}-{athlete.engine}"
        return shot(step, on=target, viewport_id=vid)

    return _snap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _card(page, name="Box Squat"):
    return page.get_by_test_id("exercise-card").filter(has_text=name)


def _open_session(athlete, plan):
    athlete.login(plan.athlete)
    athlete.page.goto(reverse("meso:athlete_session", kwargs={"pk": plan.session.pk}))
    expect(athlete.page.get_by_test_id("set-progress")).to_be_visible()


def _blur(page, press):
    """Move off the field the way an athlete does: tap/click the session note."""
    press(page.get_by_test_id("session-note-input"))


def _type_line(athlete, press, card, index, text):
    """Type `text` into the card's `index`th line, blur, wait for its POST."""
    field = card.get_by_test_id("sub-line-input").nth(index)
    press(field)
    field.fill(text)
    with athlete.page.expect_response(_is_cell_post) as info:
        _blur(athlete.page, press)
    assert info.value.ok, info.value.status
    return info.value


def _start_session(athlete, press, plan):
    """Start the session by logging a set on the RDL (leaves the squat empty)."""
    _type_line(athlete, press, _card(athlete.page, "RDL"), 0, "80 x 8")
    assert plan.session.logs.filter(athlete=plan.athlete).exists()


def _open_designer(coach_page, login, plan):
    login(plan.coach, on=coach_page)
    coach_page.goto(reverse("meso:designer_plan", kwargs={"plan_id": plan.plan.pk}))
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()


def _coach_commit(coach_page, locator, text):
    """Type into a designer line input and commit with Enter, awaiting the POST."""
    locator.click()
    locator.fill(text)
    with coach_page.expect_response(_is_cell_post) as info:
        locator.press("Enter")
    assert info.value.status in (200, 422), info.value.status
    return info.value


def _ghost(coach_page, plan):
    return coach_page.get_by_test_id(f"cell-line-new-{plan.squat.pk}")


def _squat_lines(plan):
    return list(
        Prescription.objects.filter(
            exercise_slot=plan.squat.exercise_slot, week=plan.week, line__gte=1
        )
        .exclude(text="")
        .order_by("line")
    )


def _prescribed_count(page):
    """The N of "X of N sets logged", read off the page."""
    text = page.get_by_test_id("set-progress").inner_text().strip()
    match = re.fullmatch(r"(\d+) of (\d+) sets logged", text)
    assert match, text
    return int(match.group(2))


# ---------------------------------------------------------------------------
# 1. The coach logs sets in the grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_coach_logs_sets_in_the_grid(
    athlete, viewport, snap, press, login, new_page, delivered_plan
):
    page = athlete.page
    _open_session(athlete, delivered_plan)
    total = _prescribed_count(page)
    card = _card(page)
    _type_line(athlete, press, card, 0, "225 x 5")
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"1 of {total} sets logged"
    )

    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, delivered_plan)
    pk = delivered_plan.squat.pk
    # The athlete's line is rolled up, so the ghost targets line 2.
    expect(coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")).to_be_visible()
    _coach_commit(coach_page, _ghost(coach_page, delivered_plan), "5 @ 225")
    chip2 = coach_page.get_by_test_id(f"cell-line-kind-{pk}-2")
    expect(chip2).to_be_visible()
    expect(chip2).to_have_attribute("aria-pressed", "true")
    _coach_commit(coach_page, _ghost(coach_page, delivered_plan), "5 @ 235")
    chip3 = coach_page.get_by_test_id(f"cell-line-kind-{pk}-3")
    expect(chip3).to_have_attribute("aria-pressed", "true")
    expect(chip2).to_have_attribute("aria-pressed", "true")
    snap("01-coach-logged-chips", coach_page, desktop=True)

    page.reload()
    card = _card(page)
    inputs = card.get_by_test_id("sub-line-input")
    expect(inputs.nth(0)).to_have_value("225 x 5")
    expect(inputs.nth(1)).to_have_value("5 @ 225")
    expect(inputs.nth(2)).to_have_value("5 @ 235")
    by_coach = card.get_by_test_id("sub-line-by-coach").locator("visible=true")
    expect(by_coach).to_have_count(2)
    expect(by_coach.first).to_have_text("logged by coach")
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"3 of {total} sets logged"
    )
    snap("02-athlete-sees-coach-sets", page)

    # The athlete edits one of the coach's lines; it becomes theirs.
    _type_line(athlete, press, card, 2, "235 x 4")
    expect(
        card.get_by_test_id("sub-line-by-coach").locator("visible=true")
    ).to_have_count(1)
    expect(inputs.nth(2)).to_have_value("235 x 4")
    row = inputs.nth(2).locator("xpath=..")
    expect(row.get_by_test_id("sub-line-by-coach")).to_be_hidden()
    expect(
        inputs.nth(1).locator("xpath=..").get_by_test_id("sub-line-by-coach")
    ).to_be_visible()
    snap("03-athlete-claimed-one", page)

    lines = {line.line: line for line in _squat_lines(delivered_plan)}
    assert lines[3].text == "235 x 4"
    assert lines[3].athlete_authored and not lines[3].entered_by_coach
    assert lines[2].athlete_authored and lines[2].entered_by_coach


# ---------------------------------------------------------------------------
# 2. Collision, coach second
# ---------------------------------------------------------------------------


def test_collision_coach_second_moves_the_coachs_line(
    athlete, viewport, snap, press, login, new_page, delivered_plan
):
    page = athlete.page
    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, delivered_plan)
    stale_ghost = _ghost(coach_page, delivered_plan)
    expect(stale_ghost).to_be_visible()

    _open_session(athlete, delivered_plan)
    _type_line(athlete, press, _card(page), 0, "225 x 5")
    assert [line.text for line in _squat_lines(delivered_plan)] == ["225 x 5"]

    # The coach's grid is stale: its ghost still targets line 1.
    response = _coach_commit(coach_page, stale_ghost, "brace harder")
    assert response.ok
    notice = coach_page.get_by_test_id(f"cell-notice-{delivered_plan.squat.pk}")
    expect(notice).to_be_visible()
    assert "Moved below" in notice.inner_text()
    assert "Alex" in notice.inner_text()
    snap("01-coach-moved-notice", coach_page, desktop=True)

    lines = _squat_lines(delivered_plan)
    assert [(line.line, line.text) for line in lines] == [
        (1, "225 x 5"),
        (2, "brace harder"),
    ]
    assert lines[0].athlete_authored and not lines[1].athlete_authored

    page.reload()
    card = _card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("225 x 5")
    cues = card.get_by_test_id("coach-cues")
    expect(cues).to_contain_text("from your coach")
    expect(cues.get_by_test_id("coach-cue")).to_have_text("brace harder")
    snap("02-athlete-after-reload", page)


# ---------------------------------------------------------------------------
# 3. Collision, athlete second
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_collision_athlete_second_rekeys_the_athletes_line(
    athlete, viewport, snap, press, login, new_page, delivered_plan
):
    page = athlete.page
    _open_session(athlete, delivered_plan)
    _start_session(athlete, press, delivered_plan)
    card = _card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("")

    # The coach's ghost line is the athlete's first empty squat pad line (1).
    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, delivered_plan)
    pk = delivered_plan.squat.pk
    _coach_commit(coach_page, _ghost(coach_page, delivered_plan), "5 @ 225")
    expect(coach_page.get_by_test_id(f"cell-line-kind-{pk}-1")).to_have_attribute(
        "aria-pressed", "true"
    )
    assert [(line.line, line.text) for line in _squat_lines(delivered_plan)] == [
        (1, "5 @ 225")
    ]

    # The athlete's page is stale: line 1 is still an empty pad line.
    _type_line(athlete, press, card, 0, "225 x 5")
    inputs = card.get_by_test_id("sub-line-input")
    expect(inputs.nth(0)).to_have_value("5 @ 225")
    expect(inputs.nth(1)).to_have_value("225 x 5")
    mine = inputs.nth(1).locator("xpath=..")
    coaches = inputs.nth(0).locator("xpath=..")
    expect(coaches.get_by_test_id("sub-line-by-coach")).to_be_visible()
    expect(mine.get_by_test_id("sub-line-by-coach")).to_be_hidden()
    snap("01-athlete-both-lines", page)

    lines = {line.line: line for line in _squat_lines(delivered_plan)}
    assert set(lines) == {1, 2}
    assert lines[1].text == "5 @ 225" and lines[1].entered_by_coach
    assert lines[2].text == "225 x 5" and not lines[2].entered_by_coach
    assert LoggedSet.objects.filter(source_line=lines[1]).exists()
    assert LoggedSet.objects.filter(source_line=lines[2]).exists()

    page.reload()
    total = _prescribed_count(page)
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"3 of {total} sets logged"
    )


# ---------------------------------------------------------------------------
# 4. Athlete offline variant
# ---------------------------------------------------------------------------


def test_athlete_offline_line_meets_a_coach_set(
    athlete, viewport, snap, press, login, new_page, delivered_plan
):
    page = athlete.page
    context = page.context
    _open_session(athlete, delivered_plan)
    _start_session(athlete, press, delivered_plan)
    card = _card(page)
    page.evaluate(
        "window.__e2eOnlineFired = false;"
        "window.addEventListener('online', () => { window.__e2eOnlineFired = true; });"
    )

    context.set_offline(True)
    field = card.get_by_test_id("sub-line-input").nth(0)
    press(field)
    field.fill("225 x 5")
    _blur(page, press)
    expect(field.locator("xpath=..").get_by_test_id("sub-line-queued")).to_be_visible()
    snap("01-queued-offline", page)

    # Meanwhile the coach logs a set on that same line number.
    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, delivered_plan)
    pk = delivered_plan.squat.pk
    _coach_commit(coach_page, _ghost(coach_page, delivered_plan), "5 @ 225")
    expect(coach_page.get_by_test_id(f"cell-line-kind-{pk}-1")).to_be_visible()

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    inputs = card.get_by_test_id("sub-line-input")
    expect(inputs.nth(0)).to_have_value("5 @ 225")
    expect(inputs.nth(1)).to_have_value("225 x 5")
    expect(
        card.get_by_test_id("sub-line-queued").locator("visible=true")
    ).to_have_count(0)
    expect(
        inputs.nth(0).locator("xpath=..").get_by_test_id("sub-line-by-coach")
    ).to_be_visible()
    snap("02-after-flush", page)

    assert [(line.line, line.text) for line in _squat_lines(delivered_plan)] == [
        (1, "5 @ 225"),
        (2, "225 x 5"),
    ]

    coach_page.reload()
    expect(coach_page.get_by_test_id("meso-table-view")).to_be_visible()
    expect(coach_page.get_by_test_id(f"cell-line-{pk}-1")).to_have_value("5 @ 225")
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")
    expect(marker).to_be_visible()
    marker.click()
    expect(coach_page.get_by_test_id(f"cell-line-{pk}-2")).to_have_value("225 x 5")
    snap("03-coach-sees-both", coach_page, desktop=True)


# ---------------------------------------------------------------------------
# 5. The coach's edit of an athlete line is refused visibly
# ---------------------------------------------------------------------------


def test_coach_edit_of_a_claimed_line_is_refused(
    athlete, viewport, snap, press, login, new_page, delivered_plan
):
    page = athlete.page
    _open_session(athlete, delivered_plan)
    _start_session(athlete, press, delivered_plan)

    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, delivered_plan)
    pk = delivered_plan.squat.pk
    _coach_commit(coach_page, _ghost(coach_page, delivered_plan), "5 @ 225")
    expect(coach_page.get_by_test_id(f"cell-line-kind-{pk}-1")).to_have_attribute(
        "aria-pressed", "true"
    )

    # The athlete reloads, sees the coach's line, and claims it by editing it.
    page.reload()
    card = _card(page)
    first = card.get_by_test_id("sub-line-input").first
    expect(first).to_have_value("5 @ 225")
    _type_line(athlete, press, card, 0, "225 x 6")
    claimed = _squat_lines(delivered_plan)[0]
    assert claimed.text == "225 x 6" and not claimed.entered_by_coach

    # The coach's grid is stale: it still shows its own set line at line 1.
    line1 = coach_page.get_by_test_id(f"cell-line-{pk}-1")
    response = _coach_commit(coach_page, line1, "230 x 5")
    assert response.status == 422
    refusal = coach_page.get_by_test_id(f"cell-refusal-{pk}")
    expect(refusal).to_be_visible()
    assert "230 x 5" in refusal.inner_text()
    snap("01-refusal", coach_page, desktop=True)

    add = coach_page.get_by_test_id(f"cell-refusal-add-{pk}-0")
    expect(add).to_be_visible()
    with coach_page.expect_response(_is_cell_post) as info:
        add.click()
    assert info.value.ok
    expect(refusal).to_have_count(0)
    snap("02-added-as-new-line", coach_page, desktop=True)

    lines = {line.line: line for line in _squat_lines(delivered_plan)}
    assert lines[1].text == "225 x 6"
    assert lines[1].athlete_authored and not lines[1].entered_by_coach
    others = [line for n, line in lines.items() if n != 1]
    assert [line.text for line in others] == ["230 x 5"]
