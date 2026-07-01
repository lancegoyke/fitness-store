"""Design-system phase 2 — PR A (typography) + PR B (footer chrome).

Phase 1 unified the look, and the #358 nav refresh moved the nav onto a shared
``system-ui`` stack (``static/css/nav.css``). That left the rest of the site on
``base.css``'s universal ``* { font-family: "Verdana" }``, so a modern nav sat
over Verdana body copy — the biggest remaining inconsistency.

PR A finishes the "reads as one modern site" job: introduce a ``--font`` token
(the *same* system-ui stack the nav pins) and point the universal reset at it,
so body copy matches the nav at zero network cost. The two intentional monospace
spots — the ``.box.purchase`` price button and ``.font-monospace`` code — are
preserved, as is the existing ``line-height``/``font-size`` rhythm (no type-scale
scope creep).

PR B brings the *footer* into the same fold: the bottom chrome was still on the
old ``--main-color-*`` palette (a slightly different near-black than the nav) and
its own padding. It moves onto the shared ``--nav-bg`` / ``--nav-fg`` tokens the
nav uses (so both bars are the same colour), inherits the ``--font`` from PR A
(no explicit family), and picks up the nav's ``clamp(16px, 4vw, 40px)`` inline
padding so the top and bottom chrome align to the same gutter and read as a pair.

These tests read the real ``base.css``/``nav.css``, so each is red on ``main``
and green after the corresponding change.
"""

from pathlib import Path

from django.test import SimpleTestCase

APP_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = APP_ROOT / "static" / "css"
BASE_CSS = CSS_DIR / "base.css"
NAV_CSS = CSS_DIR / "nav.css"


def _css_block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule matching ``selector``."""
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace : end + 1]


class BodyTypographyTokenTests(SimpleTestCase):
    """A ``--font`` token drives the universal reset, matching the nav."""

    def test_root_defines_a_font_token(self):
        root = _css_block(BASE_CSS.read_text(), ":root {")
        self.assertIn("--font:", root)

    def test_font_token_is_the_navs_system_ui_stack(self):
        """Reuse exactly the nav's stack so body + nav read as one face."""
        root = _css_block(BASE_CSS.read_text(), ":root {")
        for part in (
            "system-ui",
            "-apple-system",
            "Segoe UI",
            "Roboto",
            "sans-serif",
        ):
            self.assertIn(part, root)
        # ...and that really is the stack nav.css pins, so the two agree.
        self.assertIn("system-ui, -apple-system", NAV_CSS.read_text())

    def test_universal_reset_uses_the_font_token(self):
        block = _css_block(BASE_CSS.read_text(), "\n* {")
        self.assertIn("font-family: var(--font)", block)

    def test_no_bare_verdana_remains(self):
        """The lone Verdana declaration is gone (it lived only in ``* {}``)."""
        self.assertNotIn("Verdana", BASE_CSS.read_text())

    def test_universal_reset_keeps_its_rhythm(self):
        """line-height / font-size on the reset survive (no type-scale rework)."""
        block = _css_block(BASE_CSS.read_text(), "\n* {")
        self.assertIn("line-height: 1.5", block)
        self.assertIn("font-size: 16px", block)

    def test_monospace_exceptions_are_preserved(self):
        """The price button and ``.font-monospace`` code stay monospace."""
        css = BASE_CSS.read_text()
        self.assertIn("font-family: monospace", _css_block(css, ".box.purchase {"))
        self.assertIn("font-family: monospace", _css_block(css, ".font-monospace {"))


# The footer's four rules, in source order, that PR B moves onto shared tokens.
FOOTER_SELECTORS = (".footer {", ".footer a {", ".footer a:hover {", ".footer a:focus")


class FooterChromeTests(SimpleTestCase):
    """PR B: the footer reads as a pair with the refreshed nav.

    It moves off the old ``--main-color-*`` palette onto the shared nav tokens,
    inherits the ``--font`` from PR A (no explicit family), and aligns its
    horizontal padding with the nav.
    """

    def test_footer_background_uses_nav_bg_token(self):
        """The bar is the same near-black as the nav (``--nav-bg``, not #000)."""
        block = _css_block(BASE_CSS.read_text(), ".footer {")
        self.assertIn("background-color: var(--nav-bg)", block)

    def test_footer_text_uses_nav_fg_token(self):
        block = _css_block(BASE_CSS.read_text(), ".footer {")
        self.assertIn("color: var(--nav-fg)", block)

    def test_footer_links_use_nav_fg_token(self):
        block = _css_block(BASE_CSS.read_text(), ".footer a {")
        self.assertIn("color: var(--nav-fg)", block)

    def test_footer_rules_drop_the_old_palette(self):
        """No hard-coded ``--main-color-*`` anywhere in the footer chrome."""
        css = BASE_CSS.read_text()
        for selector in FOOTER_SELECTORS:
            with self.subTest(selector=selector):
                self.assertNotIn("--main-color", _css_block(css, selector))

    def test_footer_inherits_the_font_token(self):
        """No explicit family on ``.footer`` — it inherits ``* { --font }``."""
        block = _css_block(BASE_CSS.read_text(), ".footer {")
        self.assertNotIn("font-family", block)

    def test_footer_padding_matches_the_navs_inline_rhythm(self):
        """Footer picks up the nav's clamp() inline padding so the two pair."""
        gutter = "clamp(16px, 4vw, 40px)"
        self.assertIn(gutter, _css_block(BASE_CSS.read_text(), ".footer {"))
        # ...and that really is the nav's inline padding, so the two agree.
        self.assertIn(gutter, _css_block(NAV_CSS.read_text(), ".nav {"))
