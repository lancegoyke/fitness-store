# ruff: noqa: F811
"""Live session sync: coach and athlete see each other's lines unreloaded (#709, PR 2).

Both screens poll a plan change stamp (`/sync/`) every ~3s while visible and
active, and merge what they fetch without clobbering a dirty input, a queued
write or a newer write answer. No journey here reloads a page unless it says so.

The athlete half runs at the two phone sizes (390 and 360); the desktop
athlete is skipped, since PR 1's file already covers the desktop layout and
live sync is not layout-dependent. The coach is always desktop Chromium.
Journeys 1 and 2 also run the athlete on WebKit.
"""

import pytest
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog

# Reuse PR 1's plumbing: the `athlete`/`engine` fixtures and `snap`, plus the
# helpers for typing, committing and reading the squat's lines.
from e2e._coach_nav import open_latest_results
from e2e.test_meso_coach_logs_709 import ENGINES
from e2e.test_meso_coach_logs_709 import _blur
from e2e.test_meso_coach_logs_709 import _card
from e2e.test_meso_coach_logs_709 import _coach_commit
from e2e.test_meso_coach_logs_709 import _ghost
from e2e.test_meso_coach_logs_709 import _is_cell_post
from e2e.test_meso_coach_logs_709 import _open_designer
from e2e.test_meso_coach_logs_709 import _open_session
from e2e.test_meso_coach_logs_709 import _prescribed_count
from e2e.test_meso_coach_logs_709 import _squat_lines
from e2e.test_meso_coach_logs_709 import _start_session
from e2e.test_meso_coach_logs_709 import athlete  # noqa: F401  (fixture)
from e2e.test_meso_coach_logs_709 import engine  # noqa: F401  (fixture)
from e2e.test_meso_coach_logs_709 import snap  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db

LIVE = 12_000  # ms: a few poll cycles


@pytest.fixture(autouse=True)
def _phones_only(viewport):
    if not viewport["is_phone"]:
        pytest.skip("athlete side of live sync runs at the phone sizes only")


def _is_sync(response):
    return "/sync/" in response.url and response.request.method == "GET"


def _watch_sync(page):
    """Record every `/sync/` answer on `page`: {"all": n, "changed": n, "bad": [...]}."""
    seen = {"all": 0, "changed": 0, "bad": []}

    def on_response(response):
        if not _is_sync(response):
            return
        seen["all"] += 1
        if not response.ok:
            seen["bad"].append(response.status)
            return
        try:
            if response.json().get("changed"):
                seen["changed"] += 1
        except Exception:  # noqa: BLE001 - navigating away mid-read
            pass

    page.on("response", on_response)
    return seen


def _wait_changed_poll(page, seen, since, timeout=LIVE):
    """Block until `page` has had a `changed: true` poll answer past `since`."""
    page.wait_for_function("() => true")  # pump events once
    deadline_polls = timeout // 100
    for _ in range(deadline_polls):
        if seen["changed"] > since:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"no changed sync answer within {timeout}ms: {seen}")


def _empty_athlete_input(card):
    """The first line input that holds nothing (and isn't a coach line)."""
    inputs = card.get_by_test_id("sub-line-input")
    for i in range(inputs.count()):
        if inputs.nth(i).input_value() == "":
            return inputs.nth(i)
    # Past the prescribed pad lines, the athlete taps "+ add a line".
    card.get_by_role("button", name="+ add a line").click()
    return inputs.nth(inputs.count() - 1)


def _athlete_type_nowait(athlete, press, card, text):
    field = _empty_athlete_input(card)
    press(field)
    field.fill(text)
    _blur(athlete.page, press)


def _coach_type_nowait(coach_page, ghost, text):
    ghost.click()
    ghost.fill(text)
    ghost.press("Enter")


def _db_texts(plan):
    return sorted(line.text for line in _squat_lines(plan))


def _athlete_texts(card):
    inputs = card.get_by_test_id("sub-line-input")
    return [inputs.nth(i).input_value() for i in range(inputs.count())]


def _wait_db_texts(plan, athlete_page, expected, timeout=LIVE):
    for _ in range(timeout // 100):
        if _db_texts(plan) == sorted(expected):
            return
        athlete_page.wait_for_timeout(100)
    assert _db_texts(plan) == sorted(expected)


def _open_both(athlete, press, login, new_page, plan, start=True):
    _open_session(athlete, plan)
    if start:
        _start_session(athlete, press, plan)
    coach_page = new_page(desktop=True)
    _open_designer(coach_page, login, plan)
    return coach_page


# ---------------------------------------------------------------------------
# 1. The live call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_the_live_call(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    coach_seen = _watch_sync(coach_page)
    athlete_seen = _watch_sync(page)
    total = _prescribed_count(page)
    pk = plan.squat.pk
    card = _card(page)
    ghost = _ghost(coach_page, plan)
    expect(ghost).to_be_visible()

    coach_texts = ["5 @ 225", "5 @ 235", "5 @ 245"]
    athlete_texts = ["225 x 5", "230 x 5", "235 x 5"]
    for c_text, a_text in zip(coach_texts, athlete_texts):
        _coach_type_nowait(coach_page, _ghost(coach_page, plan), c_text)
        _athlete_type_nowait(athlete, press, card, a_text)

    # Nothing lost: every text, exactly once, in the DB.
    _wait_db_texts(plan, page, coach_texts + athlete_texts)

    # The athlete page converges unreloaded: all six squat lines + the starter.
    inputs = card.get_by_test_id("sub-line-input")
    page.wait_for_function(
        """(want) => {
            const vals = [...document.querySelectorAll('[data-testid="sub-line-input"]')].map(i => i.value);
            return want.every(w => vals.includes(w));
        }""",
        arg=coach_texts + athlete_texts,
        timeout=LIVE,
    )
    by_coach = card.get_by_test_id("sub-line-by-coach").locator("visible=true")
    expect(by_coach).to_have_count(3, timeout=LIVE)
    for i in range(by_coach.count()):
        assert by_coach.nth(i).inner_text().strip() == "logged by coach"
    values = _athlete_texts(card)
    for text in coach_texts + athlete_texts:
        assert values.count(text) == 1, (text, values)
    # Every coach line is shown as by-coach (the row holding the text has the label).
    for text in coach_texts:
        row = inputs.nth(values.index(text)).locator("xpath=..")
        expect(row.get_by_test_id("sub-line-by-coach")).to_be_visible()
    for text in athlete_texts:
        row = inputs.nth(values.index(text)).locator("xpath=..")
        expect(row.get_by_test_id("sub-line-by-coach")).to_be_hidden()
    # 3 + 3 on the squat + the RDL starter, all counted.
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"7 of {total} sets logged", timeout=LIVE
    )
    assert _prescribed_count(page) == total

    # The designer, unreloaded: coach lines inline with the chip, athlete roll-up.
    db_lines = {line.text: line for line in _squat_lines(plan)}
    for text in coach_texts:
        n = db_lines[text].line
        expect(coach_page.get_by_test_id(f"cell-line-{pk}-{n}")).to_have_value(
            text, timeout=LIVE
        )
        expect(coach_page.get_by_test_id(f"cell-line-kind-{pk}-{n}")).to_have_attribute(
            "aria-pressed", "true"
        )
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")
    expect(marker).to_be_visible(timeout=LIVE)
    expect(marker).to_contain_text("3 sets", timeout=LIVE)
    assert marker.inner_text().strip().startswith("✓ 3 sets"), marker.inner_text()
    assert not athlete_seen["bad"] and not coach_seen["bad"], (athlete_seen, coach_seen)
    assert coach_seen["changed"] >= 1 and athlete_seen["changed"] >= 1

    snap("01-athlete-converged", page)
    snap("02-designer-converged", coach_page, desktop=True)

    # Finish; the coach's results page shows the same count.
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/log/" in r.url
    ) as log_info:
        press(page.get_by_test_id("session-finish"))
    assert log_info.value.ok
    log = SessionLog.objects.get(session=plan.session, athlete=plan.athlete)
    assert log.status == SessionLog.Status.DONE
    assert LoggedSet.objects.filter(session_log=log).count() == 7
    open_latest_results(coach_page)
    tile = coach_page.locator(".meso-stats .meso-card").first
    assert f"7 of {total} sets logged" in " ".join(tile.inner_text().split())
    snap("03-coach-results", coach_page, desktop=True)


# ---------------------------------------------------------------------------
# 2. Collision, live
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
def test_collision_live_converges(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    pk = plan.squat.pk
    card = _card(page)
    ghost = _ghost(coach_page, plan)
    expect(ghost).to_be_visible()

    # Same line number (1) on both screens, at nearly the same moment.
    first = card.get_by_test_id("sub-line-input").first
    press(first)
    first.fill("225 x 5")
    ghost.click()
    ghost.fill("5 @ 225")
    ghost.press("Enter")
    _blur(page, press)

    _wait_db_texts(plan, page, ["225 x 5", "5 @ 225"])
    lines = _squat_lines(plan)
    assert len({line.line for line in lines}) == 2
    by_text = {line.text: line for line in lines}
    assert by_text["5 @ 225"].entered_by_coach
    assert (
        by_text["225 x 5"].athlete_authored and not by_text["225 x 5"].entered_by_coach
    )

    # Athlete: both texts, ordered by line number, same as the DB.
    expected_order = [line.text for line in lines]
    page.wait_for_function(
        """(want) => {
            const vals = [...document.querySelectorAll('[data-testid="sub-line-input"]')]
              .map(i => i.value).filter(Boolean);
            return JSON.stringify(vals.slice(0, want.length)) === JSON.stringify(want);
        }""",
        arg=expected_order,
        timeout=LIVE,
    )
    inputs = card.get_by_test_id("sub-line-input")
    assert _athlete_texts(card)[:2] == expected_order
    expect(
        card.get_by_test_id("sub-line-by-coach").locator("visible=true")
    ).to_have_count(1, timeout=LIVE)

    # Designer: the coach's line inline, the athlete's under the roll-up.
    coach_line = by_text["5 @ 225"]
    expect(
        coach_page.get_by_test_id(f"cell-line-{pk}-{coach_line.line}")
    ).to_have_value("5 @ 225", timeout=LIVE)
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")
    expect(marker).to_contain_text("1 set", timeout=LIVE)
    marker.click()
    mine = by_text["225 x 5"]
    expect(coach_page.get_by_test_id(f"cell-line-{pk}-{mine.line}")).to_have_value(
        "225 x 5", timeout=LIVE
    )
    expect(coach_page.get_by_test_id(f"cell-line-athlete-{mine.pk}")).to_be_visible()
    # Same stack on both screens.
    designer_order = [
        coach_page.get_by_test_id(f"cell-line-{pk}-{line.line}").input_value()
        for line in lines
    ]
    assert designer_order == expected_order, (designer_order, expected_order)
    snap("01-athlete-converged", page)
    snap("02-designer-converged", coach_page, desktop=True)
    assert inputs.count() >= 2


# ---------------------------------------------------------------------------
# 3. Athlete offline
# ---------------------------------------------------------------------------


def test_athlete_offline_then_back_online(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    pk = plan.squat.pk
    card = _card(page)
    page.evaluate(
        "window.__e2eOnlineFired = false;"
        "window.addEventListener('online', () => { window.__e2eOnlineFired = true; });"
    )

    page.context.set_offline(True)
    for text in ("225 x 5", "230 x 5"):
        _athlete_type_nowait(athlete, press, card, text)
    expect(
        card.get_by_test_id("sub-line-queued").locator("visible=true")
    ).to_have_count(2)
    snap("01-queued-offline", page)

    # The coach logs a set meanwhile (ghost targets line 1: the athlete's is queued).
    _coach_commit(coach_page, _ghost(coach_page, plan), "5 @ 225")
    assert _db_texts(plan) == ["5 @ 225"]
    # NOTE: it does not appear on the offline athlete page (no network).
    page.wait_for_timeout(4000)
    assert "5 @ 225" not in _athlete_texts(card)

    page.context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0",
        timeout=LIVE,
    )
    _wait_db_texts(plan, page, ["5 @ 225", "225 x 5", "230 x 5"])
    page.wait_for_function(
        """(want) => {
            const vals = [...document.querySelectorAll('[data-testid="sub-line-input"]')].map(i => i.value);
            return want.every(w => vals.filter(v => v === w).length === 1);
        }""",
        arg=["5 @ 225", "225 x 5", "230 x 5"],
        timeout=LIVE,
    )
    expect(
        card.get_by_test_id("sub-line-queued").locator("visible=true")
    ).to_have_count(0)
    snap("02-athlete-converged", page)

    # Designer unreloaded: the athlete's two lines are rolled up and readable.
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")
    expect(marker).to_contain_text("2 sets", timeout=LIVE)
    marker.click()
    for line in _squat_lines(plan):
        expect(coach_page.get_by_test_id(f"cell-line-{pk}-{line.line}")).to_have_value(
            line.text, timeout=LIVE
        )
    snap("03-designer-converged", coach_page, desktop=True)


# ---------------------------------------------------------------------------
# 4. A focused draft survives a merge
# ---------------------------------------------------------------------------


def test_coach_ghost_draft_survives_a_merge(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    coach_seen = _watch_sync(coach_page)
    pk = plan.squat.pk
    card = _card(page)
    ghost = _ghost(coach_page, plan)
    ghost.click()
    ghost.type("5 @ 2")  # partial, uncommitted, focused

    since = coach_seen["changed"]
    _athlete_type_nowait(athlete, press, card, "225 x 5")
    _wait_db_texts(plan, page, ["225 x 5"])
    _wait_changed_poll(coach_page, coach_seen, since)
    # The athlete's line arrived (roll-up) and the draft is intact and focused.
    expect(coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")).to_be_visible(
        timeout=LIVE
    )
    ghost = _ghost(coach_page, plan)
    expect(ghost).to_have_value("5 @ 2")
    expect(ghost).to_be_focused()
    snap("01-draft-after-merge", coach_page, desktop=True)

    ghost.type("45")
    with coach_page.expect_response(_is_cell_post) as info:
        ghost.press("Enter")
    assert info.value.ok
    assert _db_texts(plan) == ["225 x 5", "5 @ 245"]
    assert not coach_seen["bad"], coach_seen


def test_athlete_typed_line_survives_a_merge(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    athlete_seen = _watch_sync(page)
    card = _card(page)
    inputs = card.get_by_test_id("sub-line-input")

    # The athlete is mid-way through typing line 2 (not blurred).
    field = inputs.nth(1)
    press(field)
    field.fill("230 x")
    since = athlete_seen["changed"]

    _coach_commit(coach_page, _ghost(coach_page, plan), "5 @ 225")
    _wait_changed_poll(page, athlete_seen, since)
    # An exercise whose input has focus is not merged until the athlete leaves
    # it (#709 PR 2 review): nothing moves or changes under the cursor.
    expect(inputs.nth(1)).to_have_value("230 x")
    expect(inputs.nth(1)).to_be_focused()
    expect(inputs.nth(0)).to_have_value("")
    snap("01-typed-while-coach-logs", page)

    field = inputs.nth(1)
    field.fill("230 x 5")
    with page.expect_response(_is_cell_post) as info:
        _blur(page, press)
    assert info.value.ok
    # Out of the input, the next poll brings the coach's line in.
    expect(inputs.nth(0)).to_have_value("5 @ 225", timeout=LIVE)
    expect(inputs.nth(1)).to_have_value("230 x 5")
    snap("02-merged-after-blur", page)
    assert _db_texts(plan) == ["230 x 5", "5 @ 225"]
    assert not athlete_seen["bad"], athlete_seen


# ---------------------------------------------------------------------------
# 5. The athlete's edit of a coach set reaches the designer live
# ---------------------------------------------------------------------------


def test_athlete_edit_of_a_coach_set_reaches_the_designer(
    athlete,
    viewport,
    snap,
    press,
    login,
    new_page,
    delivered_plan,  # noqa: F811
):
    page = athlete.page
    plan = delivered_plan
    coach_page = _open_both(athlete, press, login, new_page, plan)
    pk = plan.squat.pk
    card = _card(page)

    _coach_commit(coach_page, _ghost(coach_page, plan), "5 @ 225")
    chip = coach_page.get_by_test_id(f"cell-line-kind-{pk}-1")
    expect(chip).to_have_attribute("aria-pressed", "true")

    # The coach's set reaches the athlete live; the athlete edits it.
    inputs = card.get_by_test_id("sub-line-input")
    expect(inputs.first).to_have_value("5 @ 225", timeout=LIVE)
    field = inputs.first
    press(field)
    field.fill("225 x 6")
    with page.expect_response(_is_cell_post) as info:
        _blur(page, press)
    assert info.value.ok
    claimed = _squat_lines(plan)[0]
    assert claimed.text == "225 x 6" and not claimed.entered_by_coach

    # The designer: no longer the coach's inline set; the athlete's, read-only.
    marker = coach_page.get_by_test_id(f"cell-athlete-marker-{pk}")
    expect(marker).to_contain_text("1 set", timeout=LIVE)
    if not coach_page.get_by_test_id(f"cell-line-athlete-{claimed.pk}").is_visible():
        marker.click()
    mark = coach_page.get_by_test_id(f"cell-line-athlete-{claimed.pk}")
    expect(mark).to_have_text("logged by Alex", timeout=LIVE)
    line = coach_page.get_by_test_id(f"cell-line-{pk}-{claimed.line}")
    expect(line).to_have_value("225 x 6", timeout=LIVE)
    expect(line).to_have_attribute("readonly", "")
    expect(
        coach_page.get_by_test_id(f"cell-line-kind-{pk}-{claimed.line}")
    ).to_have_count(0)
    snap("01-designer-shows-athletes-edit", coach_page, desktop=True)
    assert Prescription.objects.filter(pk=claimed.pk, athlete_authored=True).exists()
