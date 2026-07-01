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

    def test_no_zero_width_negative_margin_trick_survives(self):
        """The broken ``margin: calc(0px / 2 …)`` gutter hack is gone for good.

        (Phase-3 PR B removed the ``.form-with-sidebar`` layout — its last
        consumer, ``password_reset.html``, moved onto the auth card — so the
        newsletter's input+button spacing is now guarded by
        ``test_input_group_joins_input_and_button`` instead.)
        """
        self.assertNotIn("calc(0px / 2", _css())

    def test_footer_links_render_at_normal_weight(self):
        block = _css_block(_css(), ".footer a {")
        self.assertIn("font-weight: 400", block)
        self.assertNotIn("font-weight: 700", block)

    def test_form_control_label_is_not_bold(self):
        block = _css_block(_css(), ".form-control {")
        self.assertIn("font-weight: 400", block)

    def test_secondary_action_link_is_styled(self):
        self.assertIn("a.secondaryAction", _css())

    def test_image_placeholder_is_subtle_and_branded(self):
        """Imageless products show a soft branded watermark, not a black box."""
        css = _css()
        # anchor to the line-start rule (the clip-path rule also ends in
        # "> .image-placeholder {").
        block = _css_block(css, "\n.image-placeholder {")
        self.assertIn("var(--muted)", block)
        self.assertNotIn("var(--main-color-dark)", block)
        # the brand mark is layered in as a faded watermark via ::before
        before = _css_block(css, ".image-placeholder::before {")
        self.assertIn("favicon", before)

    def test_card_typography_is_generic_ui(self):
        """Card title sits at the body scale, description reads as muted text.

        (The title was the big page-heading size, dominating the card.)
        """
        css = _css()
        self.assertIn("font-size: var(--s0)", _css_block(css, ".card-stack h3 > a {"))
        desc = _css_block(css, ".card-stack p {")
        self.assertIn("font-size: 0.875rem", desc)
        self.assertIn("var(--main-color-gray)", desc)

    def test_focus_states_use_focus_visible(self):
        css = _css()
        # Matches the design-system :focus-visible ring pattern (PR 1).
        self.assertNotIn(".cta .button:focus,", css)
        self.assertIn(".cta .button:focus-visible,", css)
        # .box.purchase no longer carries its own :focus underline rule — it
        # animates like a button, and keyboard focus uses .button:focus-visible.
        self.assertNotIn(".box.purchase:focus {", css)

    def test_card_box_inner_boxes_are_seamless(self):
        """A product card reads as one cohesive surface.

        PR 1's global ``.box`` re-skin gave every ``.box`` its own
        border/shadow/radius, so the stacked segments inside a ``.card-box``
        (image / body / price footer) each drew a separate rounded outline.
        The inner panels must be flat — only the ``.card-box`` owns the outline.
        """
        block = _css_block(_css(), ".card-box .box {")
        self.assertIn("border: 0", block)
        self.assertIn("box-shadow: none", block)
        self.assertIn("border-radius: 0", block)
        # tighter, uniform generic-UI content inset
        self.assertIn("padding: var(--s0)", block)
        # the full-bleed image's own padding is the only gap below it (no extra
        # card-stack row gap stacked on top — that left too much space).
        self.assertIn("margin-top: 0", _css_block(_css(), ".card-stack > .frame + * {"))

    def test_detail_page_tags_reuse_the_soft_pill(self):
        """Detail-page tags get the soft accent pill, like the list/Challenges."""
        block = _css_block(_css(), ".product-switcher .tag {")
        self.assertIn("var(--accent-soft)", block)
        self.assertIn("border-radius: 999px", block)

    def test_button_links_never_underline(self):
        """<a class="button"> must not pick up the global a:hover underline."""
        self.assertIn("text-decoration: none", _css_block(_css(), ".button:focus,"))

    def test_purchase_button_animates_instead_of_underlining(self):
        """The purchase button lifts on hover (not the old link underline)."""
        block = _css_block(_css(), ".box.purchase:hover {")
        self.assertIn("box-shadow", block)
        self.assertNotIn("underline", block)

    def test_input_group_joins_input_and_button(self):
        """Newsletter input + Submit join into one control.

        Base Coat button-group: a flex row where only the outer corners stay
        rounded and the joined edges are squared off.
        """
        css = _css()
        self.assertIn("display: flex", _css_block(css, ".input-group {"))
        # the field's joined (right) edge is squared off
        self.assertIn(
            "border-start-end-radius: 0",
            _css_block(css, ".input-group > :first-child {"),
        )
        # the button's joined (left) edge is squared off
        self.assertIn(
            "border-start-start-radius: 0",
            _css_block(css, ".input-group > :last-child {"),
        )


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

    def test_newsletter_form_uses_joined_input_group(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # Email field + Submit button are joined (no gap) via the input group.
        self.assertContains(response, 'class="input-group"')
        self.assertContains(response, 'role="group"')
        self.assertContains(response, 'id="id_newsletter_email"')

    def test_home_scroll_indicator_is_not_a_box(self):
        """The hero "Scroll down" hint is decorative, not a bordered box.

        It has no action, so it carries the transparent (no-surface) treatment.
        """
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "box transparent inherit-colors")

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

    def test_detail_back_link_is_an_outline_button(self):
        response = self.client.get(
            reverse("products:program_detail", args=[self.program.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="button outline"')

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
        # Phase-3 PR B moved this page onto the auth card, so the styled submit is
        # now the full-width ``.button.block`` variant — still a styled button, the
        # property this guard exists to protect (never a raw, unstyled <button>).
        self.assertIn(
            '<button class="button block" type="submit">Change Password</button>', html
        )
        self.assertNotIn('<button type="submit">Change Password</button>', html)

    def test_tracking_result_form_renders_field_errors(self):
        class _ReqForm(forms.Form):
            score = forms.CharField(required=True)

        form = _ReqForm(data={"score": ""})
        self.assertFalse(form.is_valid())
        html = render_to_string("tracking/partials/result_form.html", {"form": form})
        self.assertIn("This field is required", html)
