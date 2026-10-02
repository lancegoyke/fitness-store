"""The designer's day cards share ONE horizontal scroller (#608 / 605.7).

Desktop only. Before, every day card owned a `.meso-table-scroll`, so once a
block had 4+ weeks and the coach scrolled sideways, "Wk 1" in Day 1 no longer
sat above "Wk 1" in Day 2. Now a single scroller wraps every day: these
journeys pin the geometry (alignment, sticky Exercise column and day headers,
one scrollbar, no sideways page scroll), keyboard focus staying visible next to
the sticky column, and drag-reorder while scrolled.

Geometry is real layout, which jsdom cannot compute, hence e2e.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import SessionSlot

pytestmark = pytest.mark.django_db

EXERCISE_COL = 264  # COL_WIDTHS.exercise in MesoTable.tsx
TOL = 0.5

# 4+ weeks is where the old per-day scrollers drifted apart. Every combination
# runs; each is ~a few seconds (weeks are added through the UI).
WIDTHS = [1512, 1440, 1280]
WEEKS = [4, 5, 6]
MATRIX = [(w, n) for w in WIDTHS for n in WEEKS]

SCROLLERS_JS = """() => {
  // Every element inside the table view that scrolls sideways on its own.
  const out = [];
  for (const el of document.querySelectorAll('[data-testid="meso-table-view"] *')) {
    const ox = getComputedStyle(el).overflowX;
    if ((ox === 'auto' || ox === 'scroll') && el.scrollWidth > el.clientWidth + 1) {
      out.push(el.className || el.tagName);
    }
  }
  // Any element with a scrolling overflow-x, scrollable or not (a second
  // `.meso-table-scroll` that merely fits is still a regression in structure).
  const declared = [...document.querySelectorAll('.meso-table-scroll')].length;
  const page = document.scrollingElement;
  return {
    scrollable: out,
    declared,
    pageScrollWidth: page.scrollWidth,
    pageClientWidth: page.clientWidth,
  };
}"""

GEOMETRY_JS = """() => {
  const sc = document.querySelector('.meso-table-scroll');
  const s = sc.getBoundingClientRect();
  const days = [...document.querySelectorAll('[data-testid^="meso-day-table-"]')].map((table) => {
    const card = table.closest('.meso-table-day');
    const header = card.querySelector('.meso-table-day-header');
    const weekLefts = [...table.querySelectorAll('[data-testid^="week-col-"]')].map(
      (th) => th.getBoundingClientRect().left
    );
    const exLefts = [
      table.querySelector('.meso-table-exercise-col'),
      ...table.querySelectorAll('.meso-table-row-name-col'),
    ].map((el) => el.getBoundingClientRect().left);
    return {
      weekLefts,
      exLefts,
      headerLeft: header.getBoundingClientRect().left,
    };
  });
  return {
    scrollerLeft: s.left,
    scrollLeft: sc.scrollLeft,
    max: sc.scrollWidth - sc.clientWidth,
    days,
  };
}"""

FOCUS_JS = """() => {
  const sc = document.querySelector('.meso-table-scroll').getBoundingClientRect();
  const el = document.activeElement;
  const r = el.getBoundingClientRect();
  return {
    testid: el.getAttribute('data-testid'),
    left: r.left, right: r.right,
    scrollerLeft: sc.left, scrollerRight: sc.right,
    inDay: (el.closest('[data-testid^="meso-day-table-"]') || {getAttribute: () => null})
      .getAttribute('data-testid'),
  };
}"""


_ENGINE = {"name": "chromium"}


@pytest.fixture(autouse=True)
def _engine(browser_name):
    _ENGINE["name"] = browser_name


def _open_designer(page, live_server, plan):
    page.goto(
        f"{live_server.url}{reverse('meso:designer_plan', kwargs={'plan_id': plan.pk})}"
    )
    expect(page.get_by_test_id("meso-table-view")).to_be_visible()


def _grow_to(page, target_weeks):
    """Click "+ Add week" until the table has `target_weeks` week columns."""
    headers = page.locator('[data-testid^="week-pill-"]')
    while headers.count() < target_weeks:
        before = headers.count()
        page.get_by_test_id("add-week").click()
        expect(headers).to_have_count(before + 1)
    # Each day table shows one header per week.
    first_day = page.locator('[data-testid^="meso-day-table-"]').first
    expect(first_day.locator('[data-testid^="week-col-"]')).to_have_count(target_weeks)


def _setup(page, live_server, login, plan, width, weeks):
    login(plan.coach)
    page.set_viewport_size({"width": width, "height": 800})
    _open_designer(page, live_server, plan)
    _grow_to(page, weeks)
    assert page.locator('[data-testid^="meso-day-table-"]').count() >= 2


def _set_scroll(page, left):
    page.evaluate(
        "(l) => { document.querySelector('.meso-table-scroll').scrollLeft = l; }", left
    )
    page.wait_for_timeout(50)


def _at_scroller_edge(left, g, slack=0):
    """Pinned to the scroller's left edge.

    Unscrolled, the card's own 1px border sits between the scroller edge and
    the (not yet stuck) column, so allow that border; once scrolled, sticky
    `left: 0` must pin it exactly.
    """
    offset = left - g["scrollerLeft"]
    if g["scrollLeft"] < 2:
        return -TOL <= offset <= 2 + TOL
    return -slack - TOL <= offset <= TOL


def _assert_aligned(page, where):
    g = page.evaluate(GEOMETRY_JS)
    first = g["days"][0]
    for i, d in enumerate(g["days"]):
        assert len(d["weekLefts"]) == len(first["weekLefts"])
        for wk, (a, b) in enumerate(zip(first["weekLefts"], d["weekLefts"]), 1):
            assert abs(a - b) <= TOL, (
                f"{where}: Wk {wk} day 1 at {a}, day {i + 1} at {b}"
            )
        for left in d["exLefts"]:
            assert _at_scroller_edge(left, g), (
                f"{where}: Exercise column of day {i + 1} at {left}, "
                f"scroller at {g['scrollerLeft']} (sticky lost)"
            )
        assert _at_scroller_edge(d["headerLeft"], g), (
            f"{where}: day {i + 1} title row at {d['headerLeft']}, "
            f"scroller at {g['scrollerLeft']} (sticky lost)"
        )
    return g


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
@pytest.mark.parametrize(("width", "weeks"), MATRIX)
def test_days_scroll_together_and_stay_aligned(
    page, live_server, login, block_plan, width, weeks
):
    _setup(page, live_server, login, block_plan.plan, width, weeks)

    info = page.evaluate(SCROLLERS_JS)
    assert info["declared"] == 1, "exactly one .meso-table-scroll"
    assert info["scrollable"] in ([], ["meso-table-scroll"]), info["scrollable"]
    # The page itself must never scroll sideways: the table scrolls inside its box.
    assert info["pageScrollWidth"] <= info["pageClientWidth"], info

    g = _assert_aligned(page, "unscrolled")
    max_scroll = g["max"]
    if max_scroll <= 1:
        # Fits without overflow (wide viewport, few weeks): alignment is all
        # there is to assert, and it just did.
        assert info["scrollable"] == []
        return
    assert info["scrollable"] == ["meso-table-scroll"]
    for frac, label in ((0.5, "half"), (1.0, "max")):
        _set_scroll(page, max_scroll * frac)
        g = _assert_aligned(page, f"{width}x{weeks} {label}")
        assert g["scrollLeft"] > 0
        # Still no sideways page scroll after scrolling the table.
        page_w = page.evaluate(SCROLLERS_JS)
        assert page_w["pageScrollWidth"] <= page_w["pageClientWidth"]


def _assert_scroll_kept(page, before_left):
    """The drag didn't move the table sideways.

    Compared against the clamped value: reordering can change the scrollable
    range slightly (WebKit's max shrank by 11px once the rows moved), and the
    browser then clamps scrollLeft to the new max.
    """
    g = page.evaluate(GEOMETRY_JS)
    # WebKit re-focuses the sticky drag handle after the drop and nudges the
    # scroller ~11px left to reveal it; Chromium doesn't move at all.
    slack = 1 if _ENGINE["name"] == "chromium" else 16
    assert abs(g["scrollLeft"] - min(before_left, g["max"])) <= slack, (before_left, g)


def _focused(page):
    # Focus-scrolling can land a frame after the key press (WebKit especially).
    page.wait_for_timeout(100)
    return page.evaluate(FOCUS_JS)


def _assert_focus_visible(page, where):
    f = _focused(page)
    assert f["testid"], f"{where}: focus is not on a testid'd element: {f}"
    # Chromium honours scroll-padding on focus-scroll; WebKit doesn't, so
    # MesoTable nudges the scroller itself — the same strict bounds hold in both.
    left_floor = f["scrollerLeft"] + EXERCISE_COL - 1
    assert f["left"] >= left_floor, (
        f"{where}: {f['testid']} hidden under the sticky Exercise column: {f}"
    )
    right_slack = 1
    assert f["right"] <= f["scrollerRight"] + right_slack, (
        f"{where}: {f['testid']} sticks out of the right edge: {f}"
    )
    return f


def _tab_until(page, predicate, limit=40, key="Tab", check=True):
    for _ in range(limit):
        page.keyboard.press(key)
        f = _focused(page)
        if f["testid"] and f["testid"].startswith("cell-") and check:
            _assert_focus_visible(page, f"after {key}")
        if predicate(f):
            return f
    raise AssertionError(f"never reached the target with {key}: {_focused(page)}")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
@pytest.mark.parametrize(("width", "weeks"), [(1280, 6), (1440, 5), (1512, 4)])
def test_keyboard_focus_stays_visible_beside_the_sticky_column(
    page, live_server, login, block_plan, width, weeks
):
    _setup(page, live_server, login, block_plan.plan, width, weeks)
    day1, day2 = page.locator('[data-testid^="meso-day-table-"]').all()[:2]
    row1_inputs = day1.locator("tbody tr").first.locator('[data-testid^="cell-text-"]')
    # Day 1 row 1 has an input in every week column it has a cell for.
    first_wk = row1_inputs.first.get_attribute("data-testid")
    last_wk = row1_inputs.last.get_attribute("data-testid")
    assert first_wk != last_wk

    day1.locator('[data-testid^="row-name-"]').first.click()
    # Tab forward: name -> tempo -> Wk 1 ... -> last week. Every cell input
    # focused on the way must be fully visible, clear of the sticky column.
    f = _tab_until(page, lambda f: f["testid"] == last_wk)
    _assert_focus_visible(page, "last week, day 1")

    # ArrowDown moves down the same column: day 1 row 2, then day 2 row 1.
    f = _tab_until(
        page,
        lambda f: f["inDay"] == day2.get_attribute("data-testid"),
        limit=12,  # ghost "+ line" inputs sit between the rows
        key="ArrowDown",
    )
    assert f["testid"].startswith("cell-")
    _assert_focus_visible(page, "last week, day 2")

    # Shift+Tab back to Wk 1 of day 2's row 1 (the same row ArrowDown landed on).
    day2_row1_inputs = day2.locator("tbody tr").first.locator(
        '[data-testid^="cell-text-"]'
    )
    target = day2_row1_inputs.first.get_attribute("data-testid")
    _tab_until(page, lambda f: f["testid"] == target, key="Shift+Tab")
    _assert_focus_visible(page, "first week, day 2")


def _rows(page, day_locator):
    return [
        t.replace("meso-row-", "")
        for t in day_locator.locator("tbody tr").evaluate_all(
            "(trs) => trs.map((t) => t.getAttribute('data-testid'))"
        )
    ]


def _drag(page, handle, target_y, target_x=None):
    box = handle.bounding_box()
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    # PointerSensor has an activation distance: nudge first, then travel.
    page.mouse.move(x, y + (4 if target_y > y else -4), steps=3)
    page.mouse.move(x + (target_x or 0), target_y, steps=20)
    page.wait_for_timeout(100)
    page.mouse.up()


def _wait_for(predicate, page, what, tries=60):
    for _ in range(tries):
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_dragging_a_row_while_scrolled_to_the_end(page, live_server, login, block_plan):
    _setup(page, live_server, login, block_plan.plan, 1280, 6)
    day1 = page.locator('[data-testid^="meso-day-table-"]').first
    max_scroll = page.evaluate(GEOMETRY_JS)["max"]
    assert max_scroll > 1
    _set_scroll(page, max_scroll)
    before_left = page.evaluate(GEOMETRY_JS)["scrollLeft"]

    first_id, second_id = _rows(page, day1)[:2]
    assert (
        ExerciseSlot.objects.get(pk=first_id).order
        < ExerciseSlot.objects.get(pk=second_id).order
    )
    first_box = day1.get_by_test_id(f"meso-row-{first_id}").bounding_box()
    _drag(
        page,
        day1.get_by_test_id(f"row-drag-{second_id}"),
        first_box["y"] + 2,
    )

    _wait_for(
        lambda: (
            ExerciseSlot.objects.get(pk=second_id).order
            < ExerciseSlot.objects.get(pk=first_id).order
        ),
        page,
        "row order to persist",
    )
    _wait_for(
        lambda: _rows(page, day1)[:2] == [second_id, first_id],
        page,
        "row order in the DOM",
    )
    _assert_scroll_kept(page, before_left)
    _assert_aligned(page, "after row drag")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_dragging_a_day_while_scrolled_to_the_end(page, live_server, login, block_plan):
    _setup(page, live_server, login, block_plan.plan, 1280, 6)
    max_scroll = page.evaluate(GEOMETRY_JS)["max"]
    _set_scroll(page, max_scroll)
    before_left = page.evaluate(GEOMETRY_JS)["scrollLeft"]

    slots = list(
        SessionSlot.objects.filter(mesocycle__plan=block_plan.plan).order_by("order")
    )
    first, second = slots[0], slots[1]
    first_box = page.get_by_test_id(f"meso-day-table-{first.pk}").bounding_box()
    _drag(
        page,
        page.get_by_test_id(f"day-drag-{second.pk}"),
        first_box["y"] - 10,
    )

    _wait_for(
        lambda: (
            SessionSlot.objects.get(pk=second.pk).order
            < SessionSlot.objects.get(pk=first.pk).order
        ),
        page,
        "day order to persist",
    )

    def _day_ids():
        return page.locator('[data-testid^="meso-day-table-"]').evaluate_all(
            "(els) => els.map((e) => e.getAttribute('data-testid'))"
        )

    # The DB lands before the client's refetch re-renders the order.
    want = [f"meso-day-table-{second.pk}", f"meso-day-table-{first.pk}"]
    _wait_for(lambda: _day_ids()[:2] == want, page, "day order in the DOM")
    _assert_scroll_kept(page, before_left)
    _assert_aligned(page, "after day drag")


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_screenshot_scrolled_to_the_end(page, live_server, login, block_plan, shot):
    _setup(page, live_server, login, block_plan.plan, 1440, 4)
    g = page.evaluate(GEOMETRY_JS)
    _set_scroll(page, g["max"])
    _assert_aligned(page, "1440x4 max")
    shot("01-scrolled-to-max", viewport_id="desktop")
