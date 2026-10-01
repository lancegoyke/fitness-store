"""#688.3 — the agent review card's Approve/Reject control never overflows.

jsdom/Django can't measure layout, so these pin the declarations that keep a
long "Honors:" line from pushing the segmented control past the card edge at
desktop widths (the old fix lived only inside the phone media query).
"""

import re
from pathlib import Path

from django.contrib.staticfiles import finders
from django.template.loader import get_template


def _rule(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"{selector} rule missing from meso.css"
    return m.group(1)


def _css():
    return Path(finders.find("css/meso.css")).read_text()


def test_review_footer_row_wraps_at_every_width():
    body = _rule(_css(), ".meso-review-foot")
    assert "flex-wrap: wrap" in body


def test_honors_pill_can_shrink_and_wrap():
    # Compound: a lone class loses to the later `.meso-badge { white-space: nowrap }`
    # (found in the browser at 1512px: the text ran out of the pill).
    body = _rule(_css(), ".meso-badge.meso-review-honors")
    assert "min-width: 0" in body
    assert "overflow-wrap: anywhere" in body
    assert "white-space: normal" in body


def test_segmented_control_keeps_its_size_and_right_edge():
    body = _rule(_css(), ".meso-review-foot > .meso-seg")
    assert "flex: 0 0 auto" in body
    assert "margin-left: auto" in body


def test_review_template_uses_the_layout_classes():
    source = get_template("meso/review.html").template.source
    assert "meso-review-foot" in source
    assert "meso-review-honors" in source


def test_athlete_marker_wraps_so_the_miss_tail_stays_visible():
    """#688: the designer marker's "· 1 miss" must not be ellipsized away."""
    css = (
        Path(__file__).resolve().parents[4]
        / "frontend/designer/src/styles/designer-mesotable.css"
    ).read_text()
    body = _rule(css, ".meso-athlete-marker")
    assert "white-space: normal" in body
    assert "text-overflow" not in body
