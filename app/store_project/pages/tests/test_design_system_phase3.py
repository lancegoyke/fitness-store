"""Design-system phase 3 — PR A (auth card shell + login page).

Phases 1–2 unified the site chrome and body copy; the **auth pages** were the
last surface still wearing the pre-unification look — ad-hoc ``.box.login``
boxes, an ``.or-separator`` built from hardcoded greys, and a
``.stack-auth-form`` layout that predates the card system.

PR A brings django-allauth's login page onto the locked "Faithful" design
language as a **basecoat-style login card**: a shared ``account/base.html`` shell
renders a centered ``.box`` card (header + body + footer slots), the login page
composes it, the social buttons migrate from ``.box.login`` anchors to
full-width ``.button.outline.block`` buttons, and ``.or-separator``'s hardcoded
greys move onto ``--muted-foreground`` / ``--border`` tokens.

The CSS guards read the real ``base.css`` and the template guards render the real
login page, so each is red on ``main`` and green after the corresponding change.
"""

from pathlib import Path

from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

APP_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = APP_ROOT / "static" / "css"
BASE_CSS = CSS_DIR / "base.css"


def _css_block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule matching ``selector``."""
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace : end + 1]


class Phase3CssTests(SimpleTestCase):
    """The tiny static-CSS gaps the login card needs (see the phase-3 plan)."""

    def test_button_block_is_full_width(self):
        """``.button.block`` spans its container (basecoat ``.btn w-full``)."""
        # A leading "\n" anchors the selector to the line start (as phase 2 does),
        # so it can't match ``.button.block:hover`` or a comment mention.
        block = _css_block(BASE_CSS.read_text(), "\n.button.block {")
        self.assertIn("width: 100%", block)

    def test_or_separator_label_uses_muted_token(self):
        """The ``or`` label is tokenized off the hardcoded ``#6f6f6f`` grey."""
        block = _css_block(BASE_CSS.read_text(), ".or-separator i {")
        self.assertIn("var(--muted-foreground)", block)
        self.assertNotIn("#6f6f6f", block)

    def test_or_separator_rule_uses_border_token(self):
        """The hairline rules are tokenized off the hardcoded ``#cac7c7`` grey."""
        block = _css_block(BASE_CSS.read_text(), ".or-separator > div {")
        self.assertIn("var(--border)", block)
        self.assertNotIn("#cac7c7", block)

    def test_legacy_separator_greys_are_gone(self):
        """Neither hardcoded separator grey survives anywhere in ``base.css``."""
        css = BASE_CSS.read_text()
        self.assertNotIn("#6f6f6f", css)
        self.assertNotIn("#cac7c7", css)

    def test_auth_card_description_uses_muted_token(self):
        """The card's muted subtitle reads off ``--muted-foreground``."""
        block = _css_block(BASE_CSS.read_text(), ".auth-card__description {")
        self.assertIn("var(--muted-foreground)", block)


class Phase3LoginTemplateTests(TestCase):
    """The login page renders the shared card, not the old ``.box.login`` look.

    ``TestCase`` (DB access) because allauth touches the session on GET.
    """

    def setUp(self):
        self.resp = self.client.get(reverse("account_login"))

    def test_login_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_login_renders_the_card_shell(self):
        """The centered auth-card wrapper + ``.box`` panel are present."""
        self.assertContains(self.resp, 'class="stack center auth-card"')
        self.assertContains(self.resp, "auth-card__panel")
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_social_buttons_are_full_width_outline_buttons(self):
        """Google + Facebook migrate to ``.button.outline.block`` (two of them)."""
        self.assertContains(self.resp, 'class="button outline block"', count=2)

    def test_primary_submit_is_a_full_width_button(self):
        """The email submit is the black full-width primary button."""
        self.assertContains(self.resp, "primaryAction button block")

    def test_old_box_login_treatment_is_gone(self):
        """The legacy ``.box.login`` social boxes no longer render on login."""
        self.assertNotContains(self.resp, "box login google")
        self.assertNotContains(self.resp, "box login facebook")

    def test_shared_nav_still_frames_the_card(self):
        """Repointing the template inheritance keeps the shared nav (#358)."""
        self.assertContains(self.resp, 'class="nav"')

    def test_form_wiring_is_preserved(self):
        """CSRF + allauth's exact field names + the forgot-password link survive."""
        self.assertContains(self.resp, "csrfmiddlewaretoken")
        self.assertContains(self.resp, 'name="login"')
        self.assertContains(self.resp, 'name="password"')
        self.assertContains(self.resp, reverse("account_reset_password"))

    def test_social_login_anchors_and_media_survive(self):
        """Both provider anchors + the providers media JS survive the migration."""
        self.assertContains(self.resp, 'id="google"')
        self.assertContains(self.resp, 'id="facebook"')
        # {% providers_media_js %} still emits the Facebook JS SDK loader.
        self.assertContains(self.resp, "connect.facebook.net")
