"""How the timer form's controls are drawn and laid out.

Contrast (issue #497): the form sits straight on the page background, with no
card behind it. With the site default (white fill, 1px ``var(--input)``
hairline) the fields were white-on-white against the idle page — roughly 1.5:1,
under the 3:1 WCAG asks for a control's edges — so they barely read as fields at
all. They now carry the same frame as the progress bar above them.

Alignment (issue #499): the rows were flex rows with ``flex: 1`` on both
children. Under ``box-sizing: border-box`` a zero flex basis is floored at the
item's own padding + border, so every control came out wider than half its row
by exactly its own padding, and the selects — which reserve 34px for the
chevron — sat 11px wider and 11px further left than the number inputs.

These read the real stylesheet, so they are red on ``main`` and green after.
"""

import re
from pathlib import Path

from django.test import TestCase

TIMER_CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "timer.css"


def _css() -> str:
    return TIMER_CSS.read_text()


def _declarations() -> str:
    """The stylesheet with comments stripped (they discuss the old rules)."""
    return re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)


def _css_block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule matching ``selector``."""
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace : end + 1]


class TimerFormContrastTests(TestCase):
    def test_fields_are_framed_like_the_progress_bar(self):
        """Fields + Reset share the bar container's border, not the hairline.

        The border has to hold up on the white idle page *and* on the blue /
        green / red washes ``.content`` takes on while a timer runs, which is
        why it matches the bar rather than only darkening a shade.
        """
        css = _css()
        bar = _css_block(css, ".bar-container {")
        fields = _css_block(css, "#timer-form .field input,")

        self.assertIn(
            "border: var(--border-thin) solid var(--main-color-dark-gray)", bar
        )
        self.assertIn(
            "border: var(--border-thin) solid var(--main-color-dark-gray)", fields
        )
        # the Reset button is in the same control group — it can't keep the
        # faint outline while the fields above it get a frame.
        self.assertIn("#timer-form .button.reset", css)

    def test_focus_still_lands_on_the_accent_border(self):
        """The ID selector outranks base.css's focus rule, so it's restated.

        Without this the focused field would keep the dark-gray border and only
        the glow ring would move.
        """
        block = _css_block(_css(), "#timer-form .field input:focus,")
        self.assertIn("border-color: var(--accent)", block)


class TimerFormAlignmentTests(TestCase):
    def test_rows_share_two_equal_columns(self):
        """Every row lays out on grid tracks, so the controls form a column.

        The track — not the control sitting in it — has to own the width, or a
        control's own padding leaks back into the layout and the rows go ragged
        again.
        """
        block = _css_block(_css(), "\n.field {")

        self.assertIn("display: grid", block)
        self.assertIn(
            "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)",
            block,
        )
        self.assertNotIn("display: flex", block)

    def test_controls_cannot_widen_their_own_column(self):
        """A number input asks for ~226px; ``minmax(0, …)`` alone isn't enough.

        Grid items floor at min-content too, so without this the input blows the
        column open (and the label's share shut) on a narrow screen.
        """
        self.assertIn("min-width: 0", _css_block(_css(), ".field > * {"))

    def test_start_and_reset_are_equal_columns_too(self):
        """Start has no border and Reset has 2px, which split them as flex."""
        block = _css_block(_css(), ".buttons {")

        self.assertIn("grid-auto-columns: minmax(0, 1fr)", block)
        self.assertNotIn("display: flex", block)

    def test_no_control_is_sized_by_flex_grow_again(self):
        """The regression guard: ``flex: 1`` is what made the rows ragged."""
        self.assertNotIn("flex: 1", _declarations())
