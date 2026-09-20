"""#572 part 2 in a real browser: focusing a moved line must not duplicate it.

A cross-day move tints a logged line, and merely focusing and leaving it must
not mint a second ``LoggedSet``.

The Django test (``app/store_project/meso/tests/test_warn_reason_after_move.py``)
covers the server side of the contract — the reason a tinted line carries is
``"elsewhere"`` after a move, not the reason an un-skip repost gets — but it
can't drive the CLIENT decision that actually prevents the duplicate:
``_lineNeedsSending`` (``app/store_project/static/js/meso_athlete.js``) reads
that reason and, only for ``"elsewhere"``, never calls ``fetch`` at all. This
test is the one that can watch that not happen. The sequence:

1. the athlete types ``225 x 5`` on Box Squat's first sub-line and blurs it —
   parse-at-commit creates a parsed ``LoggedSet``, ``source_line`` = that cell,
   on day 1's log;
2. the coach drags Box Squat to a second day through the real designer
   endpoint (``meso:api_prescription_move``, a Django test ``Client``
   force-logged-in as the coach — the coach UI isn't the subject of this
   test). The cell travels with the block-shared ``ExerciseSlot``; the
   ``LoggedSet`` stays on day 1's log (#568's decision, not touched here);
3. the athlete opens day 2's session page. The line still reads ``225 x 5``
   (its own text never changed) but now renders tinted — reachability
   evidence that the same trap #572 describes is live on this exact page;
4. the athlete focuses that line and tabs (or taps) off it without typing
   anything. Before the fix, `_lineNeedsSending` reposted every warned line
   whose text was unchanged regardless of why it warned, `_upsert_parsed_set`
   ran against day 2's log, and a SECOND ``LoggedSet`` appeared for the same
   performance while day 1's stayed, hidden, still counting toward results,
   1RM and the agent's grounding.

After the fix, step 4 never calls ``fetch`` at all, and one ``LoggedSet``
survives throughout.
"""

import json
import re

import pytest
from django.test import Client
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.tests._helpers import day

from e2e.test_meso_reclaim_restore import _blur_first_sub_line
from e2e.test_meso_reclaim_restore import _box_squat_card

pytestmark = pytest.mark.django_db

# Counts the page's fetches still in flight (mirrors test_meso_athlete_offline.py's
# INFLIGHT_JS). Added before the page loads, so it wraps every request
# `meso_athlete.js` makes — including a blur that (correctly, after the fix)
# never calls `fetch` at all, in which case this simply stays at 0 the whole
# time.
INFLIGHT_JS = """(() => {
  const fetch = window.fetch;
  window.__e2eInflight = 0;
  window.fetch = (...args) => {
    window.__e2eInflight += 1;
    return fetch(...args).finally(() => { window.__e2eInflight -= 1; });
  };
})();"""


def _move_box_squat_to(delivered_plan, target_session):
    """The coach's real cross-day drag, through the designer endpoint.

    A Django test ``Client`` force-logged-in as the coach — the coach side of
    this sequence isn't what's under test, only whether the athlete's own
    page then shows the consequence, and behaves correctly around it.
    """
    client = Client()
    client.force_login(delivered_plan.coach)
    response = client.post(
        reverse(
            "meso:api_prescription_move",
            kwargs={"plan_id": delivered_plan.plan.pk, "pk": delivered_plan.squat.pk},
        ),
        data=json.dumps({"session_id": target_session.pk, "index": 0}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response


def _focus_and_leave_without_editing(page, viewport, press, card):
    """Focus Box Squat's first sub-line and blur it WITHOUT typing anything.

    The exact gesture #572 is about — the athlete never touches the text,
    only tabs (or taps) through it. Deliberately NOT wrapped in
    ``page.expect_response`` (unlike ``_blur_first_sub_line``): the whole
    point, after the fix, is that no ``/cell/`` request is made at all.
    """
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    press(first_line)
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")


def test_focusing_a_moved_lines_line_without_editing_does_not_duplicate_the_set(
    page, viewport, shot, press, login, delivered_plan
):
    day2 = day(delivered_plan.week, day_number=2, name="Upper", bias="Push")

    page.add_init_script(INFLIGHT_JS)
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()

    # --- 1. the athlete types a set on day 1 and blurs it ---
    card = _box_squat_card(page)
    press(card.get_by_test_id("sub-line-input").first)
    _blur_first_sub_line(page, viewport, card, "225 x 5")
    assert (
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            prescription=delivered_plan.squat,
        ).count()
        == 1
    )

    # --- 2. the coach drags Box Squat to a second day ---
    _move_box_squat_to(delivered_plan, day2)

    # --- 3. the athlete opens the SECOND day's session page ---
    page.goto(reverse("meso:athlete_session", kwargs={"pk": day2.pk}))
    expect(page.get_by_role("heading", name="Upper")).to_be_visible()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    # Wait for the hydrated value FIRST — Alpine repopulates `x-model` from
    # the server payload asynchronously, and every assertion below is only
    # meaningful once that's happened.
    expect(first_line).to_have_value("225 x 5")

    # Reachability evidence: the line renders tinted on its new day, even
    # though its own text never changed — if this stopped being true, the
    # rest of the test would be exercising nothing.
    expect(card.get_by_test_id("sub-line-warn").first).to_be_visible()
    expect(first_line).to_have_class(re.compile("meso-phone-input--warn"))
    shot("01-moved-line-tinted")

    # --- 4. the athlete focuses that line and leaves it, untouched ---
    _focus_and_leave_without_editing(page, viewport, press, card)
    page.wait_for_function("() => window.__e2eInflight === 0")
    # A settling window for any stray async work the fix should never
    # schedule in the first place, rather than asserting the instant the
    # blur handler's synchronous guard returns.
    page.wait_for_timeout(300)
    shot("02-focused-and-left")

    rows = list(
        LoggedSet.objects.filter(
            session_log__session__in=[delivered_plan.session, day2],
            prescription=delivered_plan.squat,
        ).order_by("pk")
    )
    assert len(rows) == 1, (
        "focusing and leaving a moved line without editing it must not mint "
        f"a second LoggedSet: {[(r.pk, r.session_log.session_id, r.source_line_id, r.load, r.reps) for r in rows]}"
    )
    assert (rows[0].load, rows[0].reps) == ("225", "5")
    assert rows[0].session_log.session_id == delivered_plan.session.pk, (
        "the surviving row must still be the original, on day 1's log"
    )
