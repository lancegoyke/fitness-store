"""Coach runs the AI agent, reviews its proposal, and applies it (#506, third slice).

Desktop only: the composer lives in the designer's sidebar and the review
gate is a coach-only screen, so this journey overrides the shared `viewport`
fixture down to a single "desktop" parametrization instead of running the
same journey three times over (`@pytest.mark.parametrize("viewport",
["desktop"], indirect=True)` — pytest collects it once, as `[desktop]`).

The agent never calls a real model: `settings.MESO_AGENT_FAKE = True` (the
pytest-django `settings` fixture — process-wide, so the `live_server` thread
sees it too) swaps in `agent/fake.py`'s `FakeDemoClient`, a curated,
deterministic proposal. `MESO_AGENT_RUN_SYNC` is already `True` in
`config/settings/test.py`, so the whole run (ground -> propose -> validate ->
persist) happens inline inside the `agent_propose` request, before the
frontend's status poll even fires — no multi-second wait.

`FakeDemoClient.propose()` always proposes up to three changes, in the plan's
row order: a swap on the first row, a progress on the next *different* row,
and a one-set volume trim on a *third* row not already used by the other two
(`agent/fake.py`'s `_pick_trim_row`). `undelivered_plan` only has two rows
(Back Squat, Romanian Deadlift), so this test adds a real coach's second
training day with a third exercise, purely so the trim has a row of its own
— without it the batch would only ever carry two changes.

Reaches the designer the same real way `test_meso_coach_delivers.py` does
(roster -> athlete row -> profile -> "Open in designer"). After Apply, the
review screen's own JS does `window.location = data.deliver_url`, landing the
coach on the (pre-delivery) deliver screen — getting back to the designer
from there uses the site nav's own "Designer" link (present on every Meso
page via `_meso_base.html`), which resolves to the coach's one working plan,
rather than a `goto`.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.factories import CoachSubscriptionFactory
from store_project.meso.models import AgentProposalBatch
from store_project.meso.models import CoachSubscription
from store_project.meso.models import ExerciseSlot
from store_project.meso.models import Prescription
from store_project.meso.models import ProposedChange
from store_project.meso.tests._helpers import day
from store_project.meso.tests._helpers import presc

pytestmark = pytest.mark.django_db

INSTRUCTION = "Swap anything that bothers the knees and progress the loads"

# The fake agent's deterministic titles for this fixture's rows (see the
# module docstring) — computed, not guessed: agent/fake.py's
# `_pick_swap_name` skips "Box Squat" (same movement family as "Back Squat",
# both singular-fold to "squat") and picks the next candidate with no word
# overlap, "Hip Thrust"; `_bump_load` adds 2.5 to a bare (non-percent) load,
# 80 -> 82.5; the volume trim drops one set, 3 -> 2.
SWAP_TITLE = "Back Squat → Hip Thrust"
PROGRESS_TITLE = "Romanian Deadlift → 82.5"
VOLUME_TITLE = "Incline Bench Press → 2 sets"


def _chat_change_title(page, title):
    return page.locator(".meso-change-title").filter(has_text=title)


def _review_card(page, title):
    return page.get_by_test_id("review-change").filter(has_text=title)


def _decide(page, card, label):
    """Click a change card's Approve/Reject button and wait on the save."""
    with page.expect_response(
        lambda r: (
            r.request.method == "POST"
            and "/change/" in r.url
            and r.url.endswith("/status/")
        )
    ) as info:
        card.get_by_role("button", name=label).click()
    assert info.value.ok


@pytest.mark.parametrize("viewport", ["desktop"], indirect=True)
def test_coach_runs_the_agent_and_applies_its_proposal(
    page, viewport, shot, login, undelivered_plan, settings
):
    settings.MESO_AGENT_FAKE = True

    # A subscribed coach: the composer renders instead of the upgrade CTA
    # (billing/access.py's can_use_agent gate).
    CoachSubscriptionFactory(
        coach=undelivered_plan.coach, status=CoachSubscription.Status.ACTIVE
    )

    # A real coach's second training day, so the fake agent's volume trim has
    # a third row of its own — see the module docstring.
    upper = day(undelivered_plan.week, day_number=2, name="Upper", bias="Press")
    bench = presc(
        upper,
        name="Incline Bench Press",
        order=0,
        sets="3",
        reps="8",
        load="50",
        rpe="7",
    )

    squat_slot_id = undelivered_plan.squat.exercise_slot_id
    rdl_id = undelivered_plan.rdl.pk
    bench_id = bench.pk

    login(undelivered_plan.coach)

    # --- Reach the designer the real way: roster -> athlete row -> profile
    # -> "Open in designer" (same path as test_coach_edits_and_delivers). ---
    page.goto(reverse("meso:roster"))
    athlete_row = page.locator("a.meso-row").filter(has_text="Alex Athlete")
    expect(athlete_row).to_be_visible()
    athlete_row.click()

    expect(page.get_by_role("heading", name="Alex Athlete")).to_be_visible()
    open_in_designer = page.get_by_role("link", name="Open in designer")
    expect(open_in_designer).to_be_visible()
    open_in_designer.click()

    expect(page.get_by_test_id("meso-table-view")).to_be_visible()

    # --- Compose and send an instruction ---
    composer = page.get_by_test_id("agent-composer-input")
    composer.fill(INSTRUCTION)
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/agent/" in r.url
    ) as propose_info:
        page.get_by_test_id("agent-composer-send").click()
    assert propose_info.value.ok

    expect(page.get_by_text("Swapped in a joint-friendly alternative")).to_be_visible()
    expect(_chat_change_title(page, SWAP_TITLE)).to_be_visible()
    expect(_chat_change_title(page, PROGRESS_TITLE)).to_be_visible()
    expect(_chat_change_title(page, VOLUME_TITLE)).to_be_visible()

    review_link = page.get_by_test_id("agent-review-link")
    expect(review_link).to_be_visible()
    expect(review_link).to_have_text("Review 3 changes →")
    shot("01-chat-reply")

    # --- Review: approve the swap + progress, reject the volume trim ---
    review_link.click()
    expect(
        page.get_by_role("heading", name=re.compile(r"changes for Alex Athlete"))
    ).to_be_visible()

    _decide(page, _review_card(page, SWAP_TITLE), "Approve")
    _decide(page, _review_card(page, PROGRESS_TITLE), "Approve")
    _decide(page, _review_card(page, VOLUME_TITLE), "Reject")
    shot("02-review-decided")

    with page.expect_response(
        lambda r: r.request.method == "POST" and "/apply/" in r.url
    ) as apply_info:
        page.get_by_test_id("review-apply").click()
    assert apply_info.value.ok

    expect(page.get_by_role("heading", name=re.compile(r"^Deliver"))).to_be_visible()

    # Back to the designer the real way: the site nav's own "Designer" link
    # (not a goto) — it resolves to the coach's one working plan.
    page.get_by_role("link", name="Designer").click()
    expect(page.get_by_test_id("meso-table-view")).to_be_visible()

    def _assert_applied():
        expect(page.get_by_test_id(f"row-name-{squat_slot_id}")).to_have_value(
            "Hip Thrust"
        )
        expect(page.get_by_test_id(f"cell-text-{rdl_id}")).to_have_value(
            "3 x 8, RPE 8, 82.5"
        )
        # The rejected trim never applied — still 3 sets, not 2.
        expect(page.get_by_test_id(f"cell-text-{bench_id}")).to_have_value(
            "3 x 8, RPE 7, 50"
        )

    _assert_applied()
    shot("03-designer-applied")

    # A reload proves this is a real round trip, not local React state.
    page.reload()
    expect(page.get_by_test_id("meso-table-view")).to_be_visible()
    _assert_applied()

    # --- Model assertions: what the UI can't show directly ---
    batch = AgentProposalBatch.objects.get(plan=undelivered_plan.plan)
    assert batch.status == AgentProposalBatch.Status.APPLIED

    changes_by_kind = {c.kind: c for c in batch.changes.all()}
    assert (
        changes_by_kind[ProposedChange.Kind.SWAP].status
        == ProposedChange.Status.APPROVED
    )
    assert (
        changes_by_kind[ProposedChange.Kind.PROGRESS].status
        == ProposedChange.Status.APPROVED
    )
    assert (
        changes_by_kind[ProposedChange.Kind.VOLUME].status
        == ProposedChange.Status.REJECTED
    )

    # The swap is BLOCK-WIDE (P4): it renames the shared ExerciseSlot, not the
    # week's Prescription cell — the prescription id stays the same, only the
    # slot's name (and catalog link) changes.
    slot = ExerciseSlot.objects.get(pk=squat_slot_id)
    assert slot.name == "Hip Thrust"
    assert slot.exercise_id is None

    assert Prescription.objects.get(pk=rdl_id).text == "3 x 8, RPE 8, 82.5"
    assert Prescription.objects.get(pk=bench_id).text == "3 x 8, RPE 7, 50"
