"""Layout checks shared across journeys (issue #508).

Originally lived only in `test_meso_athlete_mobile.py` (the first #508
slice); the coach-mobile slice (`test_meso_coach_mobile.py`) needs the same
"does the page fit" check, so the JS + assertion live here instead of being
copied.
"""

PAGE_WIDTH_JS = """() => {
  const el = document.scrollingElement;
  return {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth};
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


def assert_fits(page):
    width = page.evaluate(PAGE_WIDTH_JS)
    assert width["scrollWidth"] <= width["clientWidth"], (
        f"page scrolls sideways: {width['scrollWidth']}px of content in a "
        f"{width['clientWidth']}px viewport"
    )
    cut_off = page.evaluate(CUT_OFF_CONTROLS_JS)
    assert cut_off == [], f"controls the athlete can't fully see: {cut_off}"
