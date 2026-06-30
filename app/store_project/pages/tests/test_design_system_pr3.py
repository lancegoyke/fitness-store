"""Design-system unification PR 3 — Meso reconnection + shared nav refactor.

PR 1 added the token layer; PR 2 cleaned up the rest of the main site. PR 3
collapses the *last* competing system: Meso's separate shell. It

* refactors the site nav (``_nav.html``) off the old Every-Layout
  ``.box.invert.navbar`` markup onto the cleaner shared-token bar validated in
  ``docs/spikes/basecoat/meso.html`` (single ``static/css/nav.css``, loaded by
  the main-site bases *and* ``_meso_base.html``),
* makes the nav work on mobile via a dependency-free CSS-only disclosure
  (a focusable checkbox toggling a burger menu),
* reuses that one nav inside the Meso coach workspace so Meso reads as part of
  Mastering Fitness (suppressed on the athlete PWA + offline surfaces, where a
  full site nav would link athletes to coach-only pages or dead offline links),
* re-points ``meso.css`` (and the standalone ``designer.html`` inline tokens)
  at the shared core token *values* — most visibly swapping the old oklch
  blue-purple accent for the site's steel-blue ``#31759d``.

These tests render the real templates / read the real stylesheets, so each is
red on ``main`` and green after the PR-3 changes.
"""

from pathlib import Path

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import TestCase
from django.urls import resolve
from django.urls import reverse

from store_project.users.factories import UserFactory

APP_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = APP_ROOT / "static" / "css"
TEMPLATES = APP_ROOT / "templates"
NAV_CSS = CSS_DIR / "nav.css"
BASE_CSS = CSS_DIR / "base.css"
MESO_CSS = CSS_DIR / "meso.css"


def _css_block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule matching ``selector``."""
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace : end + 1]


def _render_nav(user, match_url=None) -> str:
    """Render ``_nav.html`` for ``user``; set ``resolver_match`` from a URL."""
    request = RequestFactory().get(match_url or "/")
    request.user = user
    if match_url is not None:
        request.resolver_match = resolve(match_url)
    return render_to_string("_nav.html", request=request)


class SharedNavCSSTests(TestCase):
    """The one shared nav stylesheet, its tokens, and mobile behaviour."""

    def test_nav_css_file_exists(self):
        self.assertTrue(NAV_CSS.exists(), "static/css/nav.css must exist")

    def test_nav_is_a_token_driven_flex_bar(self):
        css = NAV_CSS.read_text()
        block = _css_block(css, ".nav {")
        self.assertIn("display: flex", block)
        # the bar's colours come from the shared nav tokens, not a hard #000
        self.assertIn("var(--nav-bg", block)
        self.assertIn("var(--nav-fg", _css_block(css, ".nav .link {"))

    def test_active_link_is_styled(self):
        """The current section gets an underlined affordance (spike look)."""
        block = _css_block(NAV_CSS.read_text(), ".nav .link.active {")
        self.assertIn("underline", block)

    def test_nav_has_css_only_mobile_disclosure(self):
        """A burger menu that opens with no JavaScript, and a media query."""
        css = NAV_CSS.read_text()
        self.assertIn("@media", css)
        self.assertIn(".nav-burger", css)
        # the checkbox-peer pattern reveals the links when checked
        self.assertIn(".nav-toggle:checked", css)

    def test_mobile_controls_are_scoped_to_beat_base_checkbox_rule(self):
        """Desktop must hide the toggle/burger.

        base.css styles ``input[type="checkbox"] { display: grid }`` (specificity
        0,1,1), so a bare ``.nav-toggle`` (0,1,0) loses and the checkbox shows as
        a stray box. The hide/show rules are scoped under ``.nav`` to win.
        """
        css = NAV_CSS.read_text()
        self.assertIn(".nav .nav-toggle", css)
        self.assertIn(".nav .nav-burger", css)


class SharedNavMarkupTests(TestCase):
    """``_nav.html`` rebuilt on the cleaner shared structure."""

    def test_nav_uses_spike_structure_not_every_layout(self):
        html = _render_nav(AnonymousUser())
        self.assertIn('class="nav"', html)
        self.assertIn('class="brand"', html)
        self.assertIn('class="link', html)
        # the dead Every-Layout wrappers are gone
        self.assertNotIn("nav-cluster", html)
        self.assertNotIn("menu-cluster", html)
        self.assertNotIn("nav-menu", html)

    def test_nav_has_accessible_mobile_toggle(self):
        html = _render_nav(AnonymousUser())
        self.assertIn('id="nav-toggle"', html)
        self.assertIn('class="nav-burger"', html)
        self.assertIn('aria-label="Toggle navigation menu"', html)

    def test_nav_keeps_every_site_section(self):
        html = _render_nav(AnonymousUser())
        for label in ("About", "Store", "Challenges", "Coaching", "Contact"):
            self.assertIn(f">{label}</a>", html)

    def test_nav_shows_auth_links_when_anonymous(self):
        html = _render_nav(AnonymousUser())
        self.assertIn(">Login</a>", html)
        self.assertIn(">Signup</a>", html)
        self.assertNotIn(">Logout</a>", html)

    def test_nav_shows_account_links_when_authenticated(self):
        html = _render_nav(UserFactory())
        self.assertIn(">Account</a>", html)
        self.assertIn(">Logout</a>", html)
        self.assertNotIn(">Login</a>", html)

    def test_challenges_section_highlights_on_challenges_pages(self):
        url = reverse("challenges:challenge_filtered_list")
        html = _render_nav(AnonymousUser(), match_url=url)
        self.assertIn(f'<a class="link active" href="{url}">Challenges</a>', html)

    def test_coaching_section_highlights_inside_meso(self):
        url = reverse("meso:roster")
        html = _render_nav(AnonymousUser(), match_url=url)
        self.assertIn(f'<a class="link active" href="{url}">Coaching</a>', html)

    def test_about_section_highlights_on_about_page(self):
        url = reverse("pages:single", args=["about"])
        html = _render_nav(AnonymousUser(), match_url=url)
        self.assertIn(f'<a class="link active" href="{url}">About</a>', html)


class SharedNavWiringTests(TestCase):
    """Every base that drew the old nav now loads the shared stylesheet."""

    def test_base_templates_load_shared_nav_css(self):
        for name in ("_base.html", "_base_wide.html", "_base_full.html"):
            with self.subTest(template=name):
                self.assertIn("css/nav.css", (TEMPLATES / name).read_text())
        self.assertIn("css/nav.css", (TEMPLATES / "pages" / "home.html").read_text())

    def test_base_css_defines_shared_nav_tokens(self):
        css = BASE_CSS.read_text()
        self.assertIn("--nav-bg", css)
        self.assertIn("--nav-fg", css)

    def test_base_css_drops_the_dead_every_layout_nav_rules(self):
        css = BASE_CSS.read_text()
        self.assertNotIn(".nav-cluster", css)
        self.assertNotIn(".menu-cluster", css)

    def test_footer_focus_rule_survives_navbar_removal(self):
        """Removing ``.navbar`` focus rules must not drop the footer's."""
        self.assertIn(".footer a:focus", BASE_CSS.read_text())


class MesoReconnectionTests(TestCase):
    """Meso reuses the shared nav and rides on the shared token values."""

    def _render_meso_base(self, user):
        request = RequestFactory().get("/meso/")
        request.user = user
        return render_to_string("meso/_meso_base.html", request=request)

    def test_meso_base_includes_the_shared_site_nav(self):
        html = self._render_meso_base(AnonymousUser())
        self.assertIn('class="nav"', html)  # shared bar present
        self.assertIn(">Store</a>", html)  # real site links present
        self.assertIn('class="meso-topnav"', html)  # workspace chrome kept

    def test_meso_base_loads_shared_nav_css(self):
        self.assertIn("css/nav.css", self._render_meso_base(AnonymousUser()))

    def test_meso_nav_is_overridable_via_block(self):
        """The athlete PWA + offline surfaces opt out of the site nav."""
        for name in ("athlete_home.html", "athlete_session.html", "offline.html"):
            with self.subTest(template=name):
                src = (TEMPLATES / "meso" / name).read_text()
                self.assertIn("{% block site_nav %}{% endblock %}", src)


class MesoTokenTests(TestCase):
    """``meso.css`` (and designer inline tokens) point at the shared values."""

    def test_meso_accent_is_the_shared_steel_blue(self):
        block = _css_block(MESO_CSS.read_text(), "\n.meso {")
        self.assertIn("--accent: #31759d", block)
        # the old blue-purple accent value is gone from the token declarations
        self.assertNotIn("oklch", block)

    def test_meso_neutrals_align_with_shared_tokens(self):
        block = _css_block(MESO_CSS.read_text(), "\n.meso {")
        self.assertIn("--ink: #0a0a0a", block)
        self.assertIn("--line: #e4e4e7", block)

    def test_meso_defines_the_shared_nav_tokens(self):
        block = _css_block(MESO_CSS.read_text(), "\n.meso {")
        self.assertIn("--nav-bg", block)
        self.assertIn("--nav-fg", block)

    def test_designer_inline_tokens_use_the_shared_accent(self):
        src = (TEMPLATES / "meso" / "designer.html").read_text()
        self.assertIn("#31759d", src)
        self.assertNotIn("oklch", src)
