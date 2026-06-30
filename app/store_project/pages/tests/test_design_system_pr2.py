"""Design-system unification PR 2 — rest of the main site.

PR 1 added the token layer and globally re-skinned ``.box`` / ``.button`` /
inputs / ``.tag``. PR 2 fixes the per-template inconsistencies the global
re-skin could not reach:

* the newsletter form's input + button sat flush (the ``.form-with-sidebar``
  flex wrapper zeroed its own gutters),
* footer links, the newsletter consent label, and (via inline styles) other
  weights fought the global rules,
* testimonial avatars were sized with HTML ``height``/``width`` attrs,
* the imageless-product placeholder repeated an inline ``background-color``,
* dead ``.padding`` classes and a broken ``data-productType`` attribute (which
  silenced the *book* checkout button), an unrendered tracking field error, and
  unstyled submit buttons.

These tests render the real templates / read the real stylesheet so each one is
red on ``main`` and green after the PR-2 changes.
"""

from pathlib import Path

from django import forms
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse

from store_project.products.factories import BookFactory
from store_project.products.factories import ProgramFactory
from store_project.users.factories import UserFactory

BASE_CSS = Path(__file__).resolve().parents[2] / "static" / "css" / "base.css"


def _css() -> str:
    return BASE_CSS.read_text()


def _css_block(css: str, selector: str) -> str:
    """Return the declaration body of the first rule matching ``selector``."""
    start = css.index(selector)
    brace = css.index("{", start)
    end = css.index("}", brace)
    return css[brace : end + 1]


class DesignSystemPR2CSSTests(TestCase):
    """Guards for changes that live in ``base.css`` (not visible in markup)."""

    def test_form_with_sidebar_uses_a_real_gap(self):
        """The newsletter input + Submit button must not sit flush.

        The old ``margin: calc(0px / 2 * -1)`` / ``calc(0px / 2)`` trick
        evaluated to zero, so a real ``gap`` is needed.
        """
        css = _css()
        self.assertNotIn("calc(0px / 2", css)
        block = _css_block(css, ".form-with-sidebar > * {")
        self.assertIn("gap: var(--s-1)", block)

    def test_footer_links_render_at_normal_weight(self):
        block = _css_block(_css(), ".footer a {")
        self.assertIn("font-weight: 400", block)
        self.assertNotIn("font-weight: 700", block)

    def test_form_control_label_is_not_bold(self):
        block = _css_block(_css(), ".form-control {")
        self.assertIn("font-weight: 400", block)

    def test_secondary_action_link_is_styled(self):
        self.assertIn("a.secondaryAction", _css())

    def test_image_placeholder_rule_exists(self):
        self.assertIn(".image-placeholder", _css())

    def test_focus_states_use_focus_visible(self):
        css = _css()
        # Matches the design-system :focus-visible ring pattern (PR 1).
        self.assertNotIn(".cta .button:focus,", css)
        self.assertIn(".cta .button:focus-visible,", css)
        self.assertNotIn(".box.purchase:focus {", css)
        self.assertIn(".box.purchase:focus-visible {", css)


class DesignSystemPR2TemplateTests(TestCase):
    """Render the real templates and assert the markup-level fixes."""

    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()
        cls.program = ProgramFactory()
        cls.book = BookFactory()

    def test_home_footer_has_no_inline_font_weight(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'style="font-weight:normal;"')

    def test_home_newsletter_consent_has_no_inline_font_weight(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "font-weight: unset")

    def test_home_testimonial_avatars_sized_via_css(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'height="50px"')
        self.assertNotContains(response, 'width="50px"')
        self.assertContains(response, "testimonial-avatar")

    def test_program_detail_uses_image_placeholder_class(self):
        response = self.client.get(
            reverse("products:program_detail", args=[self.program.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="image-placeholder"')
        self.assertNotContains(
            response, 'style="background-color: var(--main-color-dark);"'
        )
        # ``.padding`` (bare) is not a defined class; ``.box`` already pads.
        self.assertNotContains(response, 'class="box padding"')

    def test_book_detail_checkout_uses_kebab_case_data_attrs(self):
        """Book checkout data attrs must be kebab-case.

        ``data-productType`` lowercases to ``producttype`` in the DOM, so
        payments.js (``button.dataset.productType``) never fires the book
        checkout until the attribute is ``data-product-type``.
        """
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("products:book_detail", args=[self.book.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-product-type="book"')
        self.assertContains(response, f'data-product-slug="{self.book.slug}"')
        self.assertNotContains(response, "data-productType")
        self.assertNotContains(response, "data-productSlug")

    def test_profile_update_form_and_button_are_styled(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("users:update"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="stack-form"')
        self.assertContains(response, 'class="button" type="submit">Update')

    def test_password_reset_from_key_submit_is_a_button(self):
        class _PwForm(forms.Form):
            password1 = forms.CharField(widget=forms.PasswordInput)
            password2 = forms.CharField(widget=forms.PasswordInput)

        request = RequestFactory().get("/accounts/password/reset/key/x/")
        request.user = AnonymousUser()
        html = render_to_string(
            "account/password_reset_from_key.html",
            {"form": _PwForm(), "action_url": "/accounts/password/reset/key/x/"},
            request=request,
        )
        self.assertIn(
            '<button class="button" type="submit">Change Password</button>', html
        )
        self.assertNotIn('<button type="submit">Change Password</button>', html)

    def test_tracking_result_form_renders_field_errors(self):
        class _ReqForm(forms.Form):
            score = forms.CharField(required=True)

        form = _ReqForm(data={"score": ""})
        self.assertFalse(form.is_valid())
        html = render_to_string("tracking/partials/result_form.html", {"form": form})
        self.assertIn("This field is required", html)
