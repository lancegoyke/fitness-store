"""Coach pages on a phone, and the designer's phone fallback (issue #508, second slice).

Roster, results, the athlete profile, deliver, review and the template
library share one breakpoint (760px, `meso.css`'s `.meso-coach` section): at
every viewport the page must not scroll sideways and no control may be cut
off or run off screen; on a phone, grids that sit side by side on desktop
stack into one column and each results row stacks under its exercise name,
labelled from its `data-label`. The designer isn't editable on a phone — under
900px it shows a server-rendered message with links instead of the React
island.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect

from e2e._layout import assert_fits

pytestmark = pytest.mark.django_db

# One element, fully visible: its own content isn't clipped (scrollWidth >
# clientWidth), it doesn't run off the viewport's width, and no ancestor with
# overflow-x other than visible/auto/scroll clips it. Mirrors
# `_layout.CUT_OFF_CONTROLS_JS`'s per-element check, but for a single locator
# rather than a page-wide sweep (a results cell is a plain `div`, not one of
# that sweep's interactive-control selectors).
ELEMENT_FITS_JS = """(el) => {
  if (el.scrollWidth > el.clientWidth + 0.5) return "its own content overflows";
  const r = el.getBoundingClientRect();
  const screenWidth = document.documentElement.clientWidth;
  if (r.left < -0.5 || r.right > screenWidth + 0.5) return "runs off the screen";
  for (let box = el.parentElement; box; box = box.parentElement) {
    const overflow = getComputedStyle(box).overflowX;
    if (overflow === "auto" || overflow === "scroll") break;
    if (overflow === "visible") continue;
    const b = box.getBoundingClientRect();
    if (r.left < b.left - 0.5 || r.right > b.right + 0.5) {
      return `is cut off by its ${box.tagName.toLowerCase()}`;
    }
  }
  return null;
}"""


def _assert_fully_visible(locator, label):
    problem = locator.evaluate(ELEMENT_FITS_JS)
    assert problem is None, f"{label} {problem}"


def test_roster_page_fits_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(reverse("meso:roster"))
    expect(page.get_by_role("heading", name="Your athletes")).to_be_visible()
    shot("01-roster")

    assert_fits(page)

    # Scoped to the roster row itself, like the coach-results journey — the
    # athlete's name also appears in "Recent activity".
    athlete_row = page.locator("a.meso-row").filter(has_text="Alex Athlete")
    list_card = page.locator(".meso-card").filter(
        has=page.get_by_text("Individuals", exact=True)
    )
    # The sidebar's billing/seat summary card.
    plan_card = page.locator(".meso-card").filter(
        has=page.get_by_text("Plan", exact=True)
    )

    list_box = list_card.bounding_box()
    plan_box = plan_card.bounding_box()

    if viewport["is_phone"]:
        # On main, this page didn't scroll sideways at 360px either — the
        # athlete list column was just squeezed to ~0px behind a fixed 312px
        # sidebar. Assert what the user actually sees: the row spans most of
        # the page, and the Plan card reads as a second section below it, not
        # a sliver beside it.
        client_width = page.evaluate("document.documentElement.clientWidth")
        row_box = athlete_row.bounding_box()
        assert row_box["width"] >= 0.8 * client_width, (
            f"the athlete row is only {row_box['width']}px wide in a "
            f"{client_width}px viewport"
        )
        assert plan_box["y"] >= list_box["y"] + list_box["height"] - 1, (
            "the Plan card sits beside the athlete list instead of below it "
            f"(list bottom {list_box['y'] + list_box['height']}, plan top {plan_box['y']})"
        )
    else:
        assert plan_box["x"] >= list_box["x"] + list_box["width"] - 1, (
            "the Plan card isn't to the right of the athlete list on desktop"
        )


def test_results_page_shows_the_logged_set_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(
        reverse(
            "meso:results_session", kwargs={"session_id": coach_workspace.session.pk}
        )
    )
    expect(page.get_by_text("Logged session")).to_be_visible()
    shot("01-results")

    assert_fits(page)

    adjust_link = page.get_by_role("link", name="Adjust next week →")
    _assert_fully_visible(adjust_link, "the 'Adjust next week' topnav button")

    row = page.get_by_test_id("results-row").filter(has_text="Box Squat")
    name_cell = row.get_by_test_id("results-name")
    logged_cell = row.get_by_test_id("results-logged")
    expect(logged_cell).to_have_text("1×5 @ 100 kg")
    _assert_fully_visible(logged_cell, "the logged set")

    name_box = name_cell.bounding_box()
    logged_box = logged_cell.bounding_box()
    logged_label = logged_cell.evaluate(
        "(el) => getComputedStyle(el, '::before').content"
    )

    if viewport["is_phone"]:
        assert logged_box["y"] > name_box["y"] + name_box["height"] - 1, (
            "the logged value sits beside the exercise name instead of below it"
        )
        assert "Logged" in logged_label, (
            f"the logged cell has no phone label: {logged_label!r}"
        )
    else:
        assert abs(logged_box["y"] - name_box["y"]) < 4, (
            "the logged value isn't on the same row as the exercise name on desktop"
        )
        assert logged_box["x"] > name_box["x"], (
            "the logged cell isn't beside the exercise name on desktop"
        )
        assert logged_label == "none", (
            f"a phone label leaked onto desktop: {logged_label!r}"
        )


def test_athlete_profile_fits_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(reverse("meso:athlete", kwargs={"pk": coach_workspace.athlete.pk}))
    expect(page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    shot("01-profile")

    assert_fits(page)

    goals_card = page.locator(".meso-card").filter(
        has=page.get_by_text("Goals", exact=True)
    )
    cadence_card = page.locator(".meso-card").filter(
        has=page.get_by_text("Cadence", exact=True)
    )
    goals_box = goals_card.bounding_box()
    cadence_box = cadence_card.bounding_box()

    if viewport["is_phone"]:
        assert abs(goals_box["x"] - cadence_box["x"]) < 2, (
            "the Goals and Cadence cards don't share a left edge on a phone "
            f"(goals x={goals_box['x']}, cadence x={cadence_box['x']})"
        )
        assert abs(goals_box["width"] - cadence_box["width"]) < 2, (
            "the Goals and Cadence cards aren't the same width on a phone "
            f"(goals width={goals_box['width']}, cadence width={cadence_box['width']})"
        )
    else:
        assert cadence_box["x"] >= goals_box["x"] + goals_box["width"] - 1, (
            "the Cadence card isn't to the right of Goals on desktop"
        )


def test_deliver_page_fits_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(reverse("meso:deliver_plan", kwargs={"plan_id": coach_workspace.plan.pk}))
    expect(page.get_by_role("heading", name=re.compile(r"^Deliver"))).to_be_visible()
    shot("01-deliver")

    assert_fits(page)
    _assert_fully_visible(page.get_by_test_id("deliver-send"), "the Deliver button")


def test_review_page_fits_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(
        reverse("meso:review_batch", kwargs={"batch_id": coach_workspace.batch.pk})
    )
    expect(
        page.get_by_role("heading", name=re.compile(r"changes for Alex Athlete"))
    ).to_be_visible()
    # The "Honors" badge on the second change — proves the fixture's honored
    # change actually rendered, not just that the batch did.
    expect(page.get_by_text("Honors: lower-back fatigue note")).to_be_visible()
    shot("01-review")

    assert_fits(page)
    _assert_fully_visible(
        page.get_by_test_id("review-apply"), "the Apply & deliver button"
    )


def test_template_library_fits_on_a_phone(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(reverse("meso:template_library"))
    expect(page.get_by_role("heading", name="Templates")).to_be_visible()
    expect(page.get_by_text("Push/Pull/Legs Template")).to_be_visible()
    shot("01-templates")

    assert_fits(page)


def test_designer_phone_fallback_for_a_client_plan(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    plan_id = coach_workspace.plan.pk
    page.goto(reverse("meso:designer_plan", kwargs={"plan_id": plan_id}))

    fallback = page.get_by_test_id("designer-fallback")
    mount = page.locator("#meso-designer-root")

    if not viewport["is_phone"]:
        # Desktop: the island mounts, the fallback message stays hidden.
        expect(fallback).to_be_hidden()
        expect(page.locator(".meso-designer-root")).to_be_visible()
        return

    expect(fallback).to_be_visible()
    expect(
        page.get_by_text("Open this on a larger screen to edit the program.")
    ).to_be_visible()
    expect(mount).to_be_hidden()
    shot("01-fallback")
    assert_fits(page)

    deliver_url = reverse("meso:deliver_plan", kwargs={"plan_id": plan_id})
    expected_deliver_url = f"{deliver_url}?week={coach_workspace.week.pk}"
    profile_url = reverse("meso:athlete", kwargs={"pk": coach_workspace.athlete.pk})
    roster_url = reverse("meso:roster")

    deliver_link = fallback.get_by_role("link", name="Deliver this block")
    profile_link = fallback.get_by_role("link", name="Alex Athlete's profile")
    roster_link = fallback.get_by_role("link", name="Back to roster")
    expect(deliver_link).to_have_attribute("href", expected_deliver_url)
    expect(profile_link).to_have_attribute("href", profile_url)
    expect(roster_link).to_have_attribute("href", roster_url)

    press(deliver_link)
    expect(page).to_have_url(re.compile(re.escape(expected_deliver_url) + r"$"))
    expect(page.get_by_role("heading", name=re.compile(r"^Deliver"))).to_be_visible()

    page.goto(reverse("meso:designer_plan", kwargs={"plan_id": plan_id}))
    press(
        page.get_by_test_id("designer-fallback").get_by_role(
            "link", name="Alex Athlete's profile"
        )
    )
    expect(page).to_have_url(re.compile(re.escape(profile_url) + r"$"))
    expect(page.get_by_role("heading", name="Alex Athlete")).to_be_visible()


# The desktop side (island shown, message hidden) is covered by the client-plan
# test above; a template renders the same island.
@pytest.mark.parametrize("viewport", ["phone", "phone-360"], indirect=True)
def test_designer_phone_fallback_for_a_template(
    page, viewport, shot, press, login, coach_workspace
):
    login(coach_workspace.coach)
    page.goto(
        reverse("meso:designer_plan", kwargs={"plan_id": coach_workspace.template.pk})
    )

    fallback = page.get_by_test_id("designer-fallback")
    expect(fallback).to_be_visible()
    expect(fallback.get_by_role("link", name="Deliver this block")).to_have_count(0)
    expect(fallback.get_by_role("link", name=re.compile(r"'s profile$"))).to_have_count(
        0
    )
    expect(fallback.get_by_role("link", name="Templates")).to_have_attribute(
        "href", reverse("meso:template_library")
    )
    expect(fallback.get_by_role("link", name="Back to roster")).to_have_attribute(
        "href", reverse("meso:roster")
    )
    shot("01-fallback")
    assert_fits(page)
