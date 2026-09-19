"""Athlete journey: log a set with no signal, then reconnect (issue #506, third slice).

Athletes log sets in gyms with bad signal. `meso_athlete.js` (`save()`, ~:251)
already has an offline queue for the **structured** Set-row path: a fetch
network failure stashes the payload in `localStorage["meso-log-queue"]` and
flushes it on the window `online` event. Test A drives that queue for real, in
a real browser, with real offline network conditions (`context.set_offline`) —
nothing else in this suite exercises it.

Test B checks the *other*, newer way to log a set — a typed sub-line
(`sub-line-input` -> `saveCell` -> `_postCell` -> `athlete_cell_write`), which
does NOT enqueue on a network failure (it only sets `entry.saveError`, and
nothing retries it). Since 5a, typing a line is the main way athletes log, so
this is the offline gap that actually matters day to day. See the module-level
docstring in `meso_athlete.js` and the class docstring below for what it does
today.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import SessionLog

pytestmark = pytest.mark.django_db

# The queued/synced hints carry a curly apostrophe and a checkmark glyph in
# the template (`&#8217;`, `&#10003;`) — matched here byte for byte, not
# approximated with a straight quote.
SAVED_OFFLINE_TEXT = "Saved offline — will sync when you’re back."
SAVED_TEXT = "Saved ✓"
COULD_NOT_SAVE_TEXT = "couldn’t save"


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


@pytest.mark.xfail(
    strict=True,
    reason="typed sub-line is not queued offline — issue TBD",
)
def test_typed_line_offline_survives_reconnect(
    page, context, viewport, shot, press, login, delivered_plan
):
    """What the brief predicts: a typed-then-offline set is silently lost.

    `saveCell`/`_postCell` (meso_athlete.js ~:550) has no offline queue of its
    own — a network failure just sets `entry.saveError = true` and nothing
    retries it, ever (not on `online`, not on the next blur). `save()`'s
    payload is built from the structured Set rows only, so queuing a "Log
    session" press afterwards doesn't carry the typed text either. An athlete
    who types "100 x 5" offline and presses "Log session" ends up, after
    reconnecting, with a session marked Logged that has no logged set and an
    empty sub-line once reloaded.
    """
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

    # Blur it the way a real user would — Tab on desktop, tapping the next
    # line on phone — same as `test_meso_athlete_logging.py`.
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")

    save_error = card.get_by_test_id("sub-line-save-error").first
    expect(save_error).to_be_visible()
    expect(save_error).to_have_text(COULD_NOT_SAVE_TEXT)
    shot("01-offline-blur")

    press(page.get_by_test_id("session-log"))
    offline_msg = page.get_by_text(SAVED_OFFLINE_TEXT)
    expect(offline_msg).to_be_visible()
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("02-offline-log")

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    # The stale "couldn't save" from the sub-line write never gets retried —
    # only the structured `save()` queue flushes on `online` — so it's still
    # showing right beside the fresh "Saved (check)" from that flush.
    expect(page.get_by_text(SAVED_TEXT)).to_be_visible()
    expect(save_error).to_be_visible()
    shot("03-online")

    page.reload()
    card = _box_squat_card(page)
    first_line = card.get_by_test_id("sub-line-input").first
    shot("04-reloaded")

    # The claim under test: the typed line and its parsed set survived.
    expect(first_line).to_have_value("100 x 5")
    logged_sets = list(
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            session_log__athlete=delivered_plan.athlete,
            load="100",
            reps="5",
        )
    )
    assert logged_sets
