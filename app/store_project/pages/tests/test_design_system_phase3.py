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
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from store_project.users.factories import UserFactory

APP_ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = APP_ROOT / "static" / "css"
BASE_CSS = CSS_DIR / "base.css"
TEMPLATES_DIR = APP_ROOT / "templates"


def _template_src(relpath: str) -> str:
    """Return the raw source of a project template (for source-level guards)."""
    return (TEMPLATES_DIR / relpath).read_text()


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
    """The notice pages' copy survives every phase-3 inheritance move.

    PR A deliberately kept the notice pages on ``account/base.html`` (they
    override its ``content`` block, so the dedicated card shell — which only the
    login page extended — could not blank them). PR C migrates them onto the
    card explicitly (see ``Phase3NoticePageCardTests``). Either way, the notice's
    own copy and the shared nav must keep rendering — this guards that across
    the move.
    """

    def test_inactive_page_still_renders_its_content(self):
        resp = self.client.get(reverse("account_inactive"))
        self.assertEqual(resp.status_code, 200)
        # The notice's own copy still renders (not blanked by the shell).
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
# PR C — Second-tier auth pages onto the same card
#
# PR C brings the remaining auth surfaces onto the shared
# ``account/base_auth_card.html`` shell: the logged-in password pages
# (change / set), e-mail management + confirmation, the reset/updated notices,
# the four notice pages, and the ``socialaccount/*`` confirm / connections /
# error pages. No new CSS is added — the card gaps all shipped in PR A — so
# these are template guards only.
#
# Pages a test client can reach get render guards (the real view renders the
# real template); the handful that need OAuth/session state to reach get
# source guards that read the template file and assert it extends the card and
# fills the auth blocks. Each guard is red on ``main`` (pages still extend
# ``_base.html`` / ``account/base.html`` with the pre-card markup) and green
# after the migration.
# ---------------------------------------------------------------------------


class Phase3PasswordChangeTemplateTests(TestCase):
    """The logged-in *change password* form joins the card."""

    def setUp(self):
        self.client.force_login(UserFactory())
        self.resp = self.client.get(reverse("account_change_password"))

    def test_change_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_change_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_change_has_full_width_submit(self):
        self.assertContains(self.resp, 'class="button block"', count=1)

    def test_change_form_wiring_is_preserved(self):
        """CSRF + allauth's exact change-password field names survive."""
        self.assertContains(self.resp, "csrfmiddlewaretoken")
        self.assertContains(self.resp, 'name="oldpassword"')
        self.assertContains(self.resp, 'name="password1"')
        self.assertContains(self.resp, 'name="password2"')

    def test_change_legacy_layout_is_gone(self):
        """The pre-card ``.stack-auth-form`` / ``.login center`` markup is gone."""
        self.assertNotContains(self.resp, "stack-auth-form")
        self.assertNotContains(self.resp, 'class="login center"')

    def test_change_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')


class Phase3PasswordSetTemplateTests(TestCase):
    """The *set password* form (social-only users) joins the card.

    allauth's ``PasswordSetView`` redirects to *change* password when the user
    already has a usable password, so the fixture user's password is cleared.
    """

    def setUp(self):
        user = UserFactory()
        user.set_unusable_password()
        user.save()
        self.client.force_login(user)
        self.resp = self.client.get(reverse("account_set_password"))

    def test_set_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_set_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_set_has_full_width_submit(self):
        self.assertContains(self.resp, 'class="button block"', count=1)

    def test_set_form_wiring_is_preserved(self):
        """CSRF + allauth's exact set-password field names survive ``as_p`` drop."""
        self.assertContains(self.resp, "csrfmiddlewaretoken")
        self.assertContains(self.resp, 'name="password1"')
        self.assertContains(self.resp, 'name="password2"')

    def test_set_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')


class Phase3EmailManagementTemplateTests(TestCase):
    """The e-mail management page joins the card, preserving its action wiring."""

    def setUp(self):
        self.user = UserFactory()
        # A verified primary address so the list branch (with the action
        # buttons + status badges) renders, not just the empty-state warning.
        EmailAddress.objects.create(
            user=self.user,
            email=self.user.email,
            verified=True,
            primary=True,
        )
        self.client.force_login(self.user)
        self.resp = self.client.get(reverse("account_email"))

    def test_email_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_email_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_email_list_action_names_are_preserved(self):
        """The view dispatches on these exact submit ``name`` attributes."""
        self.assertContains(self.resp, 'name="action_primary"')
        self.assertContains(self.resp, 'name="action_send"')
        self.assertContains(self.resp, 'name="action_remove"')

    def test_email_add_form_wiring_is_preserved(self):
        self.assertContains(self.resp, "csrfmiddlewaretoken")
        self.assertContains(self.resp, 'name="action_add"')
        self.assertContains(self.resp, 'name="email"')

    def test_email_status_badges_are_soft_tags(self):
        """Verified/primary status badges reuse the shipped soft ``.tag`` pill."""
        self.assertContains(self.resp, 'class="tag"')

    def test_email_legacy_layout_is_gone(self):
        self.assertNotContains(self.resp, "stack-auth-form")

    def test_email_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')


class Phase3EmailConfirmTemplateTests(TestCase):
    """The e-mail confirmation page joins the card (invalid-key branch)."""

    def setUp(self):
        self.resp = self.client.get(
            reverse("account_confirm_email", kwargs={"key": "not-a-real-key"})
        )

    def test_confirm_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_confirm_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_confirm_invalid_key_message_survives(self):
        self.assertContains(self.resp, "expired or is invalid")

    def test_confirm_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')


class Phase3ResetNoticeTemplateTests(TestCase):
    """The two password-reset *notice* pages join the card."""

    def test_reset_done_renders_the_card(self):
        resp = self.client.get(reverse("account_reset_password_done"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, 'class="nav"')

    def test_reset_from_key_done_renders_the_card(self):
        resp = self.client.get(reverse("account_reset_password_from_key_done"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, "Your password is now changed.")
        self.assertContains(resp, 'class="nav"')


class Phase3NoticePageCardTests(TestCase):
    """Notice pages migrate from ``account/base.html`` onto the auth card.

    PR A deliberately left these on ``account/base.html`` (they override its
    ``content`` block, so the card shell would have blanked them). PR C migrates
    each one explicitly onto ``account/base_auth_card.html`` and its blocks — so
    inheritance is no longer a trap; the card is opted into by hand.
    """

    def test_inactive_joins_the_card(self):
        resp = self.client.get(reverse("account_inactive"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, "This account is inactive.")
        self.assertContains(resp, 'class="nav"')

    def test_verification_sent_joins_the_card(self):
        resp = self.client.get(reverse("account_email_verification_sent"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, 'class="nav"')


class Phase3SocialConnectionsTemplateTests(TestCase):
    """The social-account connections page joins the card."""

    def setUp(self):
        self.client.force_login(UserFactory())
        self.resp = self.client.get(reverse("socialaccount_connections"))

    def test_connections_page_renders(self):
        self.assertEqual(self.resp.status_code, 200)

    def test_connections_renders_the_card_shell(self):
        self.assertContains(self.resp, 'class="box stack auth-card__panel"')

    def test_connections_add_provider_section_survives(self):
        self.assertContains(self.resp, "socialaccount_providers")

    def test_connections_shared_nav_still_frames_the_card(self):
        self.assertContains(self.resp, 'class="nav"')


class Phase3SocialNoticeTemplateTests(TestCase):
    """The social login-cancelled + authentication-error pages join the card."""

    def test_login_cancelled_joins_the_card(self):
        resp = self.client.get(reverse("socialaccount_login_cancelled"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="box stack auth-card__panel"')
        self.assertContains(resp, 'class="nav"')

    def test_authentication_error_joins_the_card(self):
        # allauth serves the third-party auth-error page with a 401 status.
        resp = self.client.get(reverse("socialaccount_login_error"))
        self.assertEqual(resp.status_code, 401)
        self.assertContains(resp, 'class="box stack auth-card__panel"', status_code=401)
        self.assertContains(resp, 'class="nav"', status_code=401)


class Phase3AuthCardSourceGuardTests(SimpleTestCase):
    """Source guards for card pages unreachable without OAuth/signup-closed state.

    Each reads the real template and asserts it extends the shared card shell
    and fills the auth blocks (red on ``main``, green after the migration).
    """

    def test_signup_closed_extends_the_card(self):
        src = _template_src("account/signup_closed.html")
        self.assertIn('extends "account/base_auth_card.html"', src)
        self.assertIn("block auth_body", src)

    def test_verified_email_required_extends_the_card(self):
        src = _template_src("account/verified_email_required.html")
        self.assertIn('extends "account/base_auth_card.html"', src)
        self.assertIn("block auth_body", src)

    def test_social_login_confirm_extends_the_card(self):
        """The provider confirm page joins the card, keeping its element form."""
        src = _template_src("socialaccount/login.html")
        self.assertIn('extends "account/base_auth_card.html"', src)
        self.assertIn("block auth_body", src)
        self.assertIn("csrf_token", src)
        self.assertIn("block auth_title", src)

    def test_social_signup_extends_the_card(self):
        """The social signup completion form joins the card, wiring preserved."""
        src = _template_src("socialaccount/signup.html")
        self.assertIn('extends "account/base_auth_card.html"', src)
        self.assertIn("block auth_body", src)
        self.assertIn("csrf_token", src)
        self.assertIn("element fields form=form", src)
        self.assertIn("redirect_field", src)


class Phase3NoLegacyAuthLayoutTests(SimpleTestCase):
    """After PR C, no migrated auth page still hand-rolls the pre-card layout.

    The legacy ``.box.login`` social boxes and the ``.stack-auth-form`` /
    ``.login center`` form scaffolding belonged to the pre-unification look.
    They may still live in ``account/snippets/login_box.html`` (an embedded
    Google-only widget, out of scope for this phase), but none of the migrated
    *pages* should reference them.
    """

    MIGRATED_PAGES = (
        "account/password_change.html",
        "account/password_set.html",
        "account/email.html",
        "account/email_confirm.html",
        "account/password_reset_done.html",
        "account/password_reset_from_key_done.html",
        "account/account_inactive.html",
        "account/signup_closed.html",
        "account/verification_sent.html",
        "account/verified_email_required.html",
        "socialaccount/connections.html",
        "socialaccount/login.html",
        "socialaccount/signup.html",
        "socialaccount/authentication_error.html",
        "socialaccount/login_cancelled.html",
    )

    def test_migrated_pages_extend_the_card(self):
        for page in self.MIGRATED_PAGES:
            with self.subTest(page=page):
                src = _template_src(page)
                self.assertIn('extends "account/base_auth_card.html"', src)

    def test_migrated_pages_drop_stack_auth_form(self):
        for page in self.MIGRATED_PAGES:
            with self.subTest(page=page):
                self.assertNotIn("stack-auth-form", _template_src(page))
