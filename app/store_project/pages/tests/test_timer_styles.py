"""Timer form contrast (issue #497).

The timer form sits straight on the page background, with no card behind it.
With the site default (white fill, 1px ``var(--input)`` hairline) the fields
were white-on-white against the idle page — roughly 1.5:1, under the 3:1 WCAG
asks for a control's edges — so they barely read as fields at all. They now
carry the same frame as the progress bar above them.

These read the real stylesheet, so they are red on ``main`` and green after.
"""

from pathlib import Path

from django.test import TestCase

TIMER_CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "timer.css"


def _css() -> str:
    return TIMER_CSS.read_text()


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
