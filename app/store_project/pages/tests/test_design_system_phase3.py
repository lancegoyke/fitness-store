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
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from store_project.users.factories import UserFactory

APP_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = APP_ROOT / "static" / "css"
BASE_CSS = CSS_DIR / "base.css"
TEMPLATE_DIR = APP_ROOT / "templates"


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
    """The inactive notice keeps its copy + nav across the card repoint.

    PR A introduced the card via a *dedicated* ``account/base_auth_card.html`` so
    the notice pages — which then still extended ``account/base.html`` — were left
    untouched. PR C brings those notice pages onto the card explicitly (inheritance
    alone could not carry it — see ``Phase3SecondTierAuthPageTests``); this guards
    that the migration preserves the notice's own copy and the shared nav.
    """

    def test_inactive_page_still_renders_its_content(self):
        resp = self.client.get(reverse("account_inactive"))
        self.assertEqual(resp.status_code, 200)
        # The notice's own copy still renders (not blanked by the card shell).
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

    def test_signup_signin_link_preserves_next(self):
        """The header "Sign in" link keeps allauth's ?next passthrough.

        allauth exposes the redirect-preserving login URL as ``login_url``; a
        hard-coded ``{% url 'account_login' %}`` would drop the ``next`` a user
        carried in from a protected page.
        """
        resp = self.client.get(reverse("account_signup") + "?next=/dashboard/")
        self.assertContains(resp, "/accounts/login/?next=")

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


# ---------------------------------------------------------------------------
# PR C — second-tier auth pages onto the same card
#
# PR C brings the remaining allauth auth pages — password change/set, the email-
# address manager, the confirmation + notice pages, and the socialaccount confirm/
# connections pages — onto ``account/base_auth_card.html``. Template guards only
# (the card CSS all shipped in PR A): each renders the real page (or, for the pages
# the happy-path flow can't cleanly reach, the real template) and asserts the card
# wrapper plus any surviving form wiring / notice copy.
#
# NB the notice pages formerly extended ``account/base.html`` and defined their own
# ``{% block content %}``, so inheritance alone could NOT have carried the card to
# them — each was repointed onto the card's ``auth_*`` blocks explicitly.
# ``render_to_string`` guards the pages the happy path can't reach (set-password
# needs an unusable-password user; signup-closed / verified-email-required / the
# socialaccount confirm live behind flow state), matching the plan's "guard that
# each notice page actually renders the card wrapper, since inheritance won't."
# ---------------------------------------------------------------------------

CARD_PANEL = 'class="box stack auth-card__panel"'


class Phase3SecondTierAuthPageTests(TestCase):
    """The second-tier auth pages wear the shared card.

    ``TestCase`` (DB access) — allauth touches the session/DB on these GETs.
    """

    def _login(self):
        # force_login bypasses auth entirely — no password needed on the user.
        user = UserFactory()
        self.client.force_login(user)
        return user

    # --- reachable through the real view ---

    def test_password_change_renders_the_card(self):
        self._login()
        resp = self.client.get(reverse("account_change_password"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)
        self.assertContains(resp, 'class="nav"')
        # allauth's exact form wiring survives the migration.
        self.assertContains(resp, "csrfmiddlewaretoken")
        self.assertContains(resp, 'name="oldpassword"')
        self.assertContains(resp, 'name="password1"')
        self.assertContains(resp, 'name="password2"')
        # The pre-unification layout is gone.
        self.assertNotContains(resp, "stack-auth-form")

    def test_email_manager_renders_the_card(self):
        self._login()
        resp = self.client.get(reverse("account_email"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)
        self.assertContains(resp, "csrfmiddlewaretoken")
        # The add-email field survives.
        self.assertContains(resp, 'name="email"')

    def test_email_confirm_invalid_key_renders_the_card(self):
        resp = self.client.get(
            reverse("account_confirm_email", kwargs={"key": "bogus-key"})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)

    def test_password_reset_done_renders_the_card(self):
        resp = self.client.get(reverse("account_reset_password_done"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)

    def test_password_reset_from_key_done_renders_the_card(self):
        resp = self.client.get(reverse("account_reset_password_from_key_done"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)

    def test_account_inactive_renders_the_card(self):
        resp = self.client.get(reverse("account_inactive"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)
        # The notice copy survives the repoint onto the card.
        self.assertContains(resp, "This account is inactive.")

    def test_verification_sent_renders_the_card(self):
        resp = self.client.get(reverse("account_email_verification_sent"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)

    def test_socialaccount_connections_renders_the_card(self):
        self._login()
        resp = self.client.get(reverse("socialaccount_connections"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)
        # A fresh user has no linked accounts (so no CSRF-bearing remove form);
        # the always-present "add a provider" footer confirms the migration.
        self.assertContains(resp, "Add a 3rd Party Account")

    def test_socialaccount_login_cancelled_renders_the_card(self):
        resp = self.client.get(reverse("socialaccount_login_cancelled"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CARD_PANEL)

    # --- rendered directly (the happy-path flow can't reach these cleanly) ---

    def test_password_set_renders_the_card(self):
        html = render_to_string("account/password_set.html")
        self.assertIn(CARD_PANEL, html)

    def test_signup_closed_renders_the_card(self):
        html = render_to_string("account/signup_closed.html")
        self.assertIn(CARD_PANEL, html)
        # The notice copy survives.
        self.assertIn("sign up is currently closed", html)

    def test_verified_email_required_renders_the_card(self):
        html = render_to_string("account/verified_email_required.html")
        self.assertIn(CARD_PANEL, html)

    def test_socialaccount_login_confirm_renders_the_card(self):
        html = render_to_string("socialaccount/login.html")
        self.assertIn(CARD_PANEL, html)

    def test_socialaccount_authentication_error_renders_the_card(self):
        html = render_to_string("socialaccount/authentication_error.html")
        self.assertIn(CARD_PANEL, html)

    def test_socialaccount_signup_extends_the_card(self):
        """The social finish-signup form extends the card base.

        It carries a bound ``sociallogin`` form the happy path can't fake here, so
        guard the migration at the source rather than by rendering: it extends the
        card base, not the old ``_base.html`` content shell.
        """
        source = (TEMPLATE_DIR / "socialaccount" / "signup.html").read_text()
        self.assertIn('extends "account/base_auth_card.html"', source)
        self.assertNotIn('extends "_base.html"', source)
