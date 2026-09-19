"""Coach review: the Apply button's label reads as one line (issue #519).

`review.html`'s `data-testid="review-apply"` button is `.meso-btn`
(`display:inline-flex; gap:7px`), and its content — "Apply", the live
`x-text="approved"` count, and "& deliver →" — sat as three bare text/span
children, which flex blockifies into three separate flex items with a 7px
gap between each. A layout-aware read of the button's text then puts each
item on its own line instead of reading as one sentence.

`Locator.to_have_text()` can't catch this: even with `use_inner_text=True` it
COLLAPSES ALL whitespace, including newlines, before comparing, so it passes
on the broken button too. Calling `inner_text()` directly is layout-aware and
keeps those newlines, so it's the one assertion that can actually tell the
broken and fixed buttons apart.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect

pytestmark = pytest.mark.django_db


def test_review_apply_button_label_reads_as_one_line(
    page, viewport, shot, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(
        reverse("meso:review_batch", kwargs={"batch_id": coach_workspace.batch.pk})
    )
    expect(
        page.get_by_role("heading", name=re.compile(r"changes for Alex Athlete"))
    ).to_be_visible()
    shot("01-review")

    label = page.get_by_test_id("review-apply").inner_text()
    # Collapse runs of spaces/tabs to a single space, but keep newlines as
    # `inner_text()` returns them: on the broken (three-flex-item) button
    # that's "Apply\n2\n& deliver →"; on the fixed one it's a single line,
    # "Apply 2 & deliver →". Collapsing newlines too would erase that
    # difference — exactly what makes `to_have_text()` blind to this bug.
    normalized = re.sub(r"[ \t]+", " ", label).strip()
    assert normalized == "Apply 2 & deliver →"
