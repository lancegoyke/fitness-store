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

from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from store_project.users.factories import UserFactory

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


class Phase3NoticePageRegressionTests(TestCase):
    """Notice pages that still extend ``account/base.html`` are unharmed.

    PR A introduces the card via a *dedicated* ``account/base_auth_card.html``
    that only the login page extends — precisely so the notice pages, which
    still extend ``account/base.html`` and override its ``content`` block, keep
    rendering their own content instead of an empty card.
    """

    def test_inactive_page_still_renders_its_content(self):
        resp = self.client.get(reverse("account_inactive"))
        self.assertEqual(resp.status_code, 200)
        # The notice's own content block still renders (not blanked by the shell).
        self.assertContains(resp, "This account is inactive.")
        # ...and the shared nav still frames it.
        self.assertContains(resp, 'class="nav"')


# ---------------------------------------------------------------------------
# PR B — signup + password-reset twins onto the same card
#
# PR B repoints the remaining MVP auth pages (signup, the password-reset request,
# and the reset-from-key form) onto the ``account/base_auth_card.html`` shell
# introduced in PR A. No new CSS is needed — the card gaps all shipped in PR A —
# so these are template guards only: each renders the real page and asserts the
# card wrapper, the migrated buttons, and that allauth's form wiring survived.
# ---------------------------------------------------------------------------


class Phase3SignupTemplateTests(TestCase):
    """Signup joins the card — one password field, no stray forgot-pw link.

    ``TestCase`` (DB access) because allauth touches the session on GET.
    """

    def setUp(self):
        self.resp = self.client.get(reverse("account_signup"))

    def test_signup_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_signup_renders_the_card_shell(self):
        """The centered auth-card wrapper + ``.box`` panel are present."""
        self.assertContains(self.resp, 'class="stack center auth-card"')
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_signup_social_buttons_are_full_width_outline_buttons(self):
        """Google + Facebook migrate to ``.button.outline.block`` (two of them)."""
        self.assertContains(self.resp, 'class="button outline block"', count=2)

    def test_signup_primary_submit_is_a_full_width_button(self):
        """The email submit is the black full-width primary button."""
        self.assertContains(self.resp, "primaryAction button block")

    def test_signup_old_box_login_treatment_is_gone(self):
        """The legacy ``.box.login`` social boxes no longer render on signup."""
        self.assertNotContains(self.resp, "box login google")
        self.assertNotContains(self.resp, "box login facebook")

    def test_signup_has_a_single_password_field(self):
        """``ACCOUNT_SIGNUP_FIELDS`` is ``email* + password1*`` — no confirm field."""
        self.assertContains(self.resp, 'name="password1"')
        self.assertNotContains(self.resp, 'name="password2"')

    def test_signup_stray_forgot_password_link_is_gone(self):
        """Signup copy-pasted login's Forgot-password link — it must not survive."""
        self.assertNotContains(self.resp, "Forgot Password")
        self.assertNotContains(self.resp, "secondaryAction")

    def test_signup_form_wiring_is_preserved(self):
        """CSRF + allauth's exact signup field names survive the migration."""
        self.assertContains(self.resp, "csrfmiddlewaretoken")
        self.assertContains(self.resp, 'name="email"')
        self.assertContains(self.resp, 'name="password1"')

    def test_signup_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')

    def test_signup_social_anchors_and_media_survive(self):
        """Both provider anchors + the providers media JS survive the migration."""
        self.assertContains(self.resp, 'id="google"')
        self.assertContains(self.resp, 'id="facebook"')
        self.assertContains(self.resp, "connect.facebook.net")


class Phase3PasswordResetTemplateTests(TestCase):
    """The password-reset *request* page (one email field) joins the card."""

    def setUp(self):
        self.resp = self.client.get(reverse("account_reset_password"))

    def test_reset_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_reset_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="stack center auth-card"')
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_reset_has_email_field_and_full_width_submit(self):
        self.assertContains(self.resp, 'name="email"')
        self.assertContains(self.resp, 'class="button block"', count=1)

    def test_reset_form_wiring_is_preserved(self):
        self.assertContains(self.resp, "csrfmiddlewaretoken")

    def test_reset_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')

    def test_reset_has_no_social_buttons(self):
        """The request page is email-only — no social buttons, no legacy boxes."""
        self.assertNotContains(self.resp, 'class="button outline block"')
        self.assertNotContains(self.resp, "box login google")


class Phase3PasswordResetFromKeyTemplateTests(TestCase):
    """The reset-from-key page joins the card in both of its branches.

    allauth renders this template two ways: a ``token_fail`` notice for an
    invalid key, and the two-password form for a valid key (reached after the
    session-storing redirect to the ``set-password`` URL). Both must wear the
    card.
    """

    def test_bad_token_renders_card_with_message(self):
        """A bogus key falls to the ``token_fail`` branch inside the card."""
        user = UserFactory()
        url = reverse(
            "account_reset_password_from_key",
            kwargs={"uidb36": user_pk_to_url_str(user), "key": "invalid-key"},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, "Bad Token")
        self.assertContains(resp, 'class="nav"')

    def test_valid_key_renders_card_with_two_password_fields(self):
        """A valid key reaches the set-password form (password1 + password2)."""
        user = UserFactory()
        url = reverse(
            "account_reset_password_from_key",
            kwargs={
                "uidb36": user_pk_to_url_str(user),
                "key": default_token_generator.make_token(user),
            },
        )
        # allauth stores the key in the session and redirects to the keyless
        # ``set-password`` URL, which renders the form — follow that hop.
        resp = self.client.get(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, 'name="password1"')
        self.assertContains(resp, 'name="password2"')
        self.assertContains(resp, 'class="button block"', count=1)
        self.assertContains(resp, "csrfmiddlewaretoken")
