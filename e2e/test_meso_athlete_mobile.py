"""Athlete pages on a phone (issue #508, first slice).

The athlete opens their training home, reads the block, switches weeks with
the chips, and taps into a session to log it. At every viewport the page must
not scroll sideways or cut off a control; on a phone the block reads as
stacked cards (one line
per sub-line) instead of a wide table, every input is big enough that iOS
Safari won't zoom on focus, and every control is a comfortable tap target.

Chromium emulating a phone is not iOS Safari, so the zoom check is the
computed font size (16px is Safari's threshold), not the zoom itself.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db

# Apple's minimum comfortable tap target, in CSS px.
MIN_TAP = 44

# The narrowest phone still in use; checked on top of each phone viewport.
NARROWEST_PHONE = {"width": 320, "height": 640}

PAGE_WIDTH_JS = """() => {
  const el = document.scrollingElement;
  return {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth};
}"""

# Every rendered control narrower or shorter than MIN_TAP, as readable labels.
# A control inside a display:none ancestor has an empty rect and is skipped.
SMALL_TAP_TARGETS_JS = """(minTap) => {
  const selector = 'a[href], button, input:not([type=hidden]), select, textarea, summary';
  const small = [];
  for (const el of document.querySelectorAll(selector)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (getComputedStyle(el).visibility === 'hidden') continue;
    if (r.width + 0.5 < minTap || r.height + 0.5 < minTap) {
      const label = (el.innerText || el.placeholder || el.getAttribute('aria-label') || '')
        .trim().replace(/\\s+/g, ' ').slice(0, 30);
      small.push(`${el.tagName.toLowerCase()} "${label}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
  }
  return small;
}"""

# Every rendered control that sticks out of the screen, or out of a box that
# clips it (a card with overflow:hidden cuts it off rather than letting the
# page scroll — which is how the old set row failed at 320px). A box that
# scrolls on purpose (overflow auto/scroll) ends the search.
CUT_OFF_CONTROLS_JS = """() => {
  const selector = 'a[href], button, input:not([type=hidden]), select, textarea, summary';
  const screenWidth = document.documentElement.clientWidth;
  const cutOff = [];
  for (const el of document.querySelectorAll(selector)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const label = (el.innerText || el.placeholder || el.getAttribute('aria-label') || '')
      .trim().replace(/\\s+/g, ' ').slice(0, 30);
    const name = `${el.tagName.toLowerCase()} "${label}"`;
    if (r.left < -0.5 || r.right > screenWidth + 0.5) {
      cutOff.push(`${name} runs off the screen`);
      continue;
    }
    for (let box = el.parentElement; box; box = box.parentElement) {
      const overflow = getComputedStyle(box).overflowX;
      if (overflow === 'auto' || overflow === 'scroll') break;
      if (overflow === 'visible') continue;
      const b = box.getBoundingClientRect();
      if (r.left < b.left - 0.5 || r.right > b.right + 0.5) {
        cutOff.push(`${name} is cut off by its ${box.tagName.toLowerCase()}`);
        break;
      }
    }
  }
  return cutOff;
}"""

INPUT_FONT_SIZES_JS = """() => [...document.querySelectorAll('input, textarea, select')]
  .filter((el) => el.type !== 'hidden')
  .map((el) => ({
    label: el.placeholder || el.getAttribute('aria-label') || el.name || el.type,
    size: parseFloat(getComputedStyle(el).fontSize),
  }))"""


def _assert_fits(page):
    width = page.evaluate(PAGE_WIDTH_JS)
    assert width["scrollWidth"] <= width["clientWidth"], (
        f"page scrolls sideways: {width['scrollWidth']}px of content in a "
        f"{width['clientWidth']}px viewport"
    )
    cut_off = page.evaluate(CUT_OFF_CONTROLS_JS)
    assert cut_off == [], f"controls the athlete can't fully see: {cut_off}"


def _assert_fits_down_to_320(page, viewport):
    """No sideways scroll and no cut-off control, here and (phone) at 320px."""
    _assert_fits(page)
    if viewport["is_phone"]:
        size = page.viewport_size
        page.set_viewport_size(NARROWEST_PHONE)
        try:
            _assert_fits(page)
        finally:
            page.set_viewport_size(size)


def _assert_tap_targets(page, viewport):
    if not viewport["is_phone"]:
        return
    small = page.evaluate(SMALL_TAP_TARGETS_JS, MIN_TAP)
    assert small == [], f"controls smaller than {MIN_TAP}x{MIN_TAP}px: {small}"


def _stacked_lines(page, exercise):
    row = page.get_by_test_id("block-stack-row").filter(has_text=exercise)
    return row.get_by_test_id("block-stack-line")


def test_athlete_reads_their_block_week_by_week(
    page, viewport, shot, press, login, block_plan
):
    login(block_plan.athlete)
    page.goto(reverse("meso:athlete_home"))
    expect(page.get_by_role("heading", name="Your programs")).to_be_visible()
    shot("01-home")

    _assert_fits_down_to_320(page, viewport)
    _assert_tap_targets(page, viewport)

    if not viewport["is_phone"]:
        # Wide screens keep the multi-week table, one column per week.
        table = page.get_by_test_id("block-table").first
        expect(table).to_be_visible()
        expect(table.get_by_role("columnheader")).to_have_count(4)
        expect(page.get_by_test_id("block-stack-row").first).to_be_hidden()
        return

    # A phone gets the stacked view instead: the selected week's prescription,
    # one line per sub-line — the four-line squat reads as four lines, each
    # below the last, not one " · "-joined string.
    expect(page.get_by_test_id("block-table").first).to_be_hidden()
    lines = _stacked_lines(page, "Back Squat")
    expect(lines).to_have_text(block_plan.squat_lines[1])
    tops = [lines.nth(i).bounding_box()["y"] for i in range(4)]
    assert tops == sorted(tops) and len(set(tops)) == 4, (
        f"the four prescription lines don't sit on four separate lines: {tops}"
    )
    expect(page.get_by_test_id("block-stack-row").filter(has_text="·")).to_have_count(0)

    # The week chips drive the stacked view: tapping "Wk 2" shows week 2's
    # prescription.
    press(page.get_by_role("link", name=re.compile(r"^Wk 2\b")))
    expect(page).to_have_url(re.compile(r"\?week=\d+$"))
    expect(_stacked_lines(page, "Back Squat")).to_have_text(block_plan.squat_lines[2])
    shot("02-week-2")
    _assert_fits_down_to_320(page, viewport)


def test_athlete_opens_a_session_from_home(
    page, viewport, shot, press, login, block_plan
):
    login(block_plan.athlete)
    page.goto(reverse("meso:athlete_home"))

    # The same way in as a real athlete: tap the day on the training home.
    press(page.get_by_role("link", name=re.compile(r"Lower")))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()
    squat = page.get_by_test_id("exercise-card").filter(has_text="Back Squat")
    # Alpine renders the cards from the JSON payload; wait for the coach's
    # first sub-line to hydrate before measuring anything.
    expect(squat.get_by_test_id("sub-line-input").first).to_have_value("RPE 7")
    shot("01-session")

    _assert_fits_down_to_320(page, viewport)
    _assert_tap_targets(page, viewport)

    # iOS Safari zooms the page when an input under 16px takes focus. Every
    # input here — the %1RM box, the typed sub-lines, load/reps/rpe — is 16px.
    sizes = page.evaluate(INPUT_FONT_SIZES_JS)
    kinds = {s["label"] for s in sizes}
    assert {"load", "reps", "rpe"} <= kinds and len(sizes) > 10, sizes
    small_inputs = [s for s in sizes if s["size"] < 16]
    assert small_inputs == [], f"inputs that make iOS zoom on focus: {small_inputs}"

    # A realistic set typed into the first row stays readable: every value is
    # fully visible in its box, and the %1RM estimate it produces (Back Squat
    # is prescribed at 70%) fits beside or below the row without pushing
    # anything off screen.
    first_set = squat.locator(".meso-set-row").first
    for placeholder, value in (("load", "102.5"), ("reps", "10"), ("rpe", "8.5")):
        first_set.get_by_placeholder(placeholder).fill(value)
    expect(first_set.get_by_text("1RM ≈")).to_be_visible()
    hidden_text = first_set.locator("input").evaluate_all(
        "(inputs) => inputs.filter((el) => el.scrollWidth > el.clientWidth)"
        ".map((el) => `${el.placeholder}: ${el.value}`)"
    )
    assert hidden_text == [], f"typed values cut off in their boxes: {hidden_text}"
    shot("02-set-typed")
    _assert_fits_down_to_320(page, viewport)

    # "Log session" is a full-size button the athlete can actually reach:
    # tall enough to hit, and nothing is laid over it once it's scrolled to.
    log_button = page.get_by_test_id("session-log")
    log_button.scroll_into_view_if_needed()
    box = log_button.bounding_box()
    if viewport["is_phone"]:
        assert box["height"] >= MIN_TAP, f"Log session is {box['height']}px tall"
    on_top = page.evaluate(
        """([x, y]) => {
          const el = document.elementFromPoint(x, y);
          return !!el && !!el.closest('[data-testid="session-log"]');
        }""",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
    )
    assert on_top, "something covers the Log session button"
