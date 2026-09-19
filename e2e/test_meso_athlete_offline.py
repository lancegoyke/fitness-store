"""Athlete journey: log a set with no signal, then reconnect (issue #506, third slice).

Athletes log in gyms with bad signal, and a set lost offline is the worst
failure the athlete app can have. `meso_athlete.js` queues a "Log session" /
"Save progress" that can't reach the server: `save()` stashes its payload in
`localStorage["meso-log-queue"]` and `flushQueue()` replays it on the window
`online` event. The first test drives that queue in a real browser with the
network really cut (`context.set_offline`).

That queue only carries the structured Set rows (load × reps). A line typed
under "what you did" (`sub-line-input` → `saveCell` → `_postCell` →
`athlete_cell_write`), the main way athletes log since 5a, isn't queued: a
failed write shows "couldn't save" and nothing retries it on reconnect. The
second test asserts what the athlete needs from that path and is a strict
xfail on #527.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog

pytestmark = pytest.mark.django_db

# The template writes these with `&#8217;` and `&#10003;`, so they're matched
# with the curly apostrophe and the check mark, not a straight quote.
SAVED_OFFLINE_TEXT = "Saved offline — will sync when you’re back."
SAVED_TEXT = "Saved ✓"


def _box_squat_card(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Box Squat")


def _online_watcher(page):
    """Arm a flag this test can poll to prove Chromium fired `online`.

    `context.set_offline(False)` is a CDP-level network-condition change, not
    a real network interface event, so whether Chromium's `online`/`offline`
    DOM events actually follow it is worth confirming in the browser rather
    than assuming.
    """
    page.evaluate(
        "window.__e2eOnlineFired = false;"
        "window.addEventListener('online', () => { window.__e2eOnlineFired = true; });"
    )


def test_athlete_logs_a_set_offline_and_it_syncs(
    page, context, viewport, shot, press, login, delivered_plan
):
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    _online_watcher(page)

    context.set_offline(True)

    # Fill the Box Squat's first Set row (the structured path `save()`
    # actually POSTs) — scoped to its card, `.first()` picks set 1.
    card = _box_squat_card(page)
    load_input = card.get_by_placeholder("load").first
    reps_input = card.get_by_placeholder("reps").first
    press(load_input)
    load_input.fill("100")
    press(reps_input)
    reps_input.fill("5")

    press(page.get_by_test_id("session-log"))

    offline_msg = page.get_by_text(SAVED_OFFLINE_TEXT)
    expect(offline_msg).to_be_visible()
    # "Log session" flips the local badge at once, offline or not (save()'s
    # `if (markDone) this.status = "done"` runs before the fetch attempt).
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("01-offline-queued")

    queue = page.evaluate("JSON.parse(localStorage.getItem('meso-log-queue') || '[]')")
    assert len(queue) == 1
    body = queue[0]["body"]
    assert body["status"] == "done"
    assert any(s["load"] == "100" and s["reps"] == "5" for s in body["sets"]), body[
        "sets"
    ]

    # Nothing has reached the server yet — the queue is the only copy.
    assert not SessionLog.objects.filter(
        session=delivered_plan.session,
        athlete=delivered_plan.athlete,
        status=SessionLog.Status.DONE,
    ).exists()
    assert not LoggedSet.objects.filter(
        session_log__session=delivered_plan.session,
        session_log__athlete=delivered_plan.athlete,
        load="100",
    ).exists()

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)

    expect(offline_msg).to_be_hidden()
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    # `flushQueue` sets `saved = true` in the same synchronous block that
    # empties the queue (before its own 2.4s auto-hide `setTimeout`), so it's
    # still up right after the queue-empty wait above resolves.
    expect(page.get_by_text(SAVED_TEXT)).to_be_visible()
    shot("02-synced")

    page.reload()
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    card = _box_squat_card(page)
    expect(card.get_by_placeholder("load").first).to_have_value("100")
    expect(card.get_by_placeholder("reps").first).to_have_value("5")
    shot("03-reloaded")

    log = (
        SessionLog.objects.filter(
            session=delivered_plan.session, athlete=delivered_plan.athlete
        )
        .order_by("-created_at")
        .first()
    )
    assert log is not None
    assert log.status == SessionLog.Status.DONE
    logged_sets = list(log.sets.all())
    assert any(s.load == "100" and s.reps == "5" for s in logged_sets), logged_sets


# Counts the page's fetches still in flight. Added before the page loads, so
# it wraps every request `meso_athlete.js` makes. The xfail test waits for it
# to reach 0 before reloading, so whatever a fix sends on reconnect has landed
# before the page is read back.
INFLIGHT_JS = """(() => {
  const fetch = window.fetch;
  window.__e2eInflight = 0;
  window.fetch = (...args) => {
    window.__e2eInflight += 1;
    return fetch(...args).finally(() => { window.__e2eInflight -= 1; });
  };
})();"""


@pytest.mark.xfail(
    strict=True,
    reason="#527: a line typed offline isn't queued or retried on reconnect",
)
def test_typed_line_offline_survives_reconnect(
    page, context, viewport, shot, press, login, delivered_plan
):
    """A line typed offline is saved once the athlete is back online.

    Only the outcome is asserted, never today's broken intermediate states
    ("couldn't save" beside the line, "Saved ✓" beside that). A fix may show
    something else on the way, and a strict xfail that pinned today's
    behavior would keep failing after the fix instead of XPASSing.

    For whoever fixes #527: a fix that retries the line on `online` after
    `flushQueue()` finishes makes this pass at every viewport. One that sends
    both at once doesn't, here. `live_server` on the in-memory SQLite test
    database shares one connection across its request threads, so two
    requests at the same moment collide ("no such savepoint", a 500) and the
    queued log never drains. Postgres in production has no such problem.
    """
    page.add_init_script(INFLIGHT_JS)
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    _online_watcher(page)

    context.set_offline(True)

    card = _box_squat_card(page)
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    press(first_line)
    first_line.fill("100 x 5")
    # Blur it the way a real user would, as `test_meso_athlete_logging.py`
    # does: Tab on desktop, tap the next line on phone.
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")

    press(page.get_by_test_id("session-log"))
    expect(page.get_by_text(SAVED_OFFLINE_TEXT)).to_be_visible()
    shot("01-offline")

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    shot("02-back-online")

    # Back online, nothing may still say the line didn't save. Today this is
    # where it fails: "couldn't save" stays beside the line (#527). 3s, not
    # the 5s default, keeps the expected failure cheap at three viewports.
    expect(card.get_by_test_id("sub-line-save-error").first).to_be_hidden(timeout=3000)
    page.wait_for_function("() => window.__e2eInflight === 0")

    page.reload()
    card = _box_squat_card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("100 x 5")
    shot("03-reloaded")
    assert LoggedSet.objects.filter(
        session_log__session=delivered_plan.session,
        session_log__athlete=delivered_plan.athlete,
        load="100",
        reps="5",
    ).exists()
