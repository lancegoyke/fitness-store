"""Athlete journey: log a set with no signal, then reconnect (issue #506, third slice).

Athletes log in gyms with bad signal, and a set lost offline is the worst
failure the athlete app can have. `meso_athlete.js` queues a "Log session" /
"Save progress" that can't reach the server: `save()` stashes its payload in
`localStorage["meso-log-queue"]` and `flushQueue()` replays it on the window
`online` event. The first test drives that queue in a real browser with the
network really cut (`context.set_offline`).

A line typed under "what you did" (`sub-line-input` → `saveCell` →
`_postCell` → `athlete_cell_write`), the main way athletes log since 5a, goes
through the same queue (#527): a write that can't reach the server is kept as a
`kind: "cell"` entry and the line says "saved offline — will sync". The
typed-line tests cover a reconnect with the page still open, a reconnect on
the next visit after the page was closed offline, and a line retyped offline
before it ever synced.
"""

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import LoggedSet
from store_project.meso.models import Prescription
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
# it wraps every request `meso_athlete.js` makes. The tests below wait for it
# to reach 0 before reloading (or opening a fresh page on the same context),
# so whatever a fix sends on reconnect has landed before the page is read
# back.
INFLIGHT_JS = """(() => {
  const fetch = window.fetch;
  window.__e2eInflight = 0;
  window.fetch = (...args) => {
    window.__e2eInflight += 1;
    return fetch(...args).finally(() => { window.__e2eInflight -= 1; });
  };
})();"""


def test_typed_line_offline_survives_reconnect(
    page, context, viewport, shot, press, login, delivered_plan
):
    """A line typed offline is queued, says so, and syncs on reconnect (#527).

    The line shows "saved offline — will sync", never "couldn't save", and
    the page doesn't say "Saved ✓" while it waits. On reconnect the line's
    write reaches the server before the queued log does. Sending them one at a
    time also matters here: `live_server` on the in-memory SQLite test
    database shares one connection across its request threads, so two
    requests at the same moment collide ("no such savepoint", a 500).
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
    # does: Tab on desktop, tap the next line on phone. The SECOND line is
    # only ever tabbed/tapped THROUGH — untouched — so it must pick up
    # neither marker: it was never written to, offline or not.
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")

    expect(card.get_by_test_id("sub-line-queued").first).to_be_visible()
    expect(card.get_by_test_id("sub-line-save-error").first).to_be_hidden()
    second_line_row = sub_lines.nth(1).locator("xpath=..")
    expect(second_line_row.get_by_test_id("sub-line-queued")).to_be_hidden()
    expect(second_line_row.get_by_test_id("sub-line-save-error")).to_be_hidden()
    shot("00-offline-line-queued")

    press(page.get_by_test_id("session-log"))
    expect(page.get_by_text(SAVED_OFFLINE_TEXT)).to_be_visible()
    expect(page.get_by_text(SAVED_TEXT)).to_be_hidden()
    shot("01-offline")

    # Recorded BEFORE going back online, so it captures every POST the
    # reconnect makes — including the very first one.
    post_urls = []
    page.on(
        "request",
        lambda req: post_urls.append(req.url) if req.method == "POST" else None,
    )

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    # Everything landed, so now the page may say so.
    expect(page.get_by_text(SAVED_TEXT)).to_be_visible()
    shot("02-back-online")

    # Nothing may still say the line didn't save, and the "queued" marker
    # comes down once it's actually synced.
    expect(card.get_by_test_id("sub-line-save-error").first).to_be_hidden(timeout=3000)
    expect(card.get_by_test_id("sub-line-queued").first).to_be_hidden(timeout=3000)
    page.wait_for_function("() => window.__e2eInflight === 0")

    cell_posts = [u for u in post_urls if "/cell/" in u]
    log_posts = [u for u in post_urls if "/log/" in u]
    assert cell_posts, f"no cell POST recorded: {post_urls}"
    assert log_posts, f"no log POST recorded: {post_urls}"
    assert post_urls.index(cell_posts[0]) < post_urls.index(log_posts[0]), (
        f"the cell write must sync before the queued log: {post_urls}"
    )

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


def _squat_line_cell(delivered_plan, line=1):
    return Prescription.objects.get(
        exercise_slot=delivered_plan.squat.exercise_slot,
        week=delivered_plan.squat.week,
        line=line,
    )


def test_typed_line_offline_syncs_on_next_visit(
    page, context, viewport, shot, press, login, delivered_plan
):
    """A line queued offline syncs on the next visit (the `init()` flush).

    The athlete closes the page while still offline, so the page's `online`
    listener never gets to run. Opening the session again later, online, is
    what recovers it: `init()` shows the queued text on its line and flushes
    it, with nobody pressing "Log session".
    """
    context.add_init_script(INFLIGHT_JS)  # covers this page AND the next one
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    expect(page.get_by_test_id("session-status")).to_have_text("To do")

    context.set_offline(True)

    card = _box_squat_card(page)
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first
    press(first_line)
    first_line.fill("100 x 5")
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")
    expect(card.get_by_test_id("sub-line-queued").first).to_be_visible()
    shot("00-offline-line-queued")

    page.close()  # the athlete left the gym without ever coming back online

    context.set_offline(False)
    new_page = context.new_page()  # same context → same localStorage/cookies
    new_page.goto(
        reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk})
    )
    card = new_page.get_by_test_id("exercise-card").filter(has_text="Box Squat")

    # init()'s fold-in: the line reads the queued text right away, THEN syncs.
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("100 x 5")
    new_page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    expect(card.get_by_test_id("sub-line-queued").first).to_be_hidden()
    new_page.wait_for_function("() => window.__e2eInflight === 0")
    shot("01-folded-in-and-synced", on=new_page, viewport_id=viewport["id"])

    new_page.reload()
    card = new_page.get_by_test_id("exercise-card").filter(has_text="Box Squat")
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("100 x 5")
    shot("02-reloaded", on=new_page, viewport_id=viewport["id"])

    assert LoggedSet.objects.filter(
        session_log__session=delivered_plan.session,
        session_log__athlete=delivered_plan.athlete,
        load="100",
        reps="5",
    ).exists()
    # Nobody ever pressed "Log session" — only the cell write replayed.
    log = SessionLog.objects.get(
        session=delivered_plan.session, athlete=delivered_plan.athlete
    )
    assert log.status == SessionLog.Status.PENDING


def test_typed_line_retyped_offline_syncs_last_text(
    page, context, viewport, shot, press, login, delivered_plan
):
    """A line retyped offline before it synced: the last text lands, once.

    The queue keeps one entry per line, so the second blur replaces the
    first rather than queueing a second set, and the server ends up with
    exactly one `LoggedSet` for the line.
    """
    page.add_init_script(INFLIGHT_JS)
    login(delivered_plan.athlete)
    page.goto(reverse("meso:athlete_session", kwargs={"pk": delivered_plan.session.pk}))
    _online_watcher(page)

    context.set_offline(True)

    card = _box_squat_card(page)
    sub_lines = card.get_by_test_id("sub-line-input")
    first_line = sub_lines.first

    press(first_line)
    first_line.fill("100 x 5")
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")
    expect(card.get_by_test_id("sub-line-queued").first).to_be_visible()

    # Back to the SAME line, retype it, and blur again — still offline.
    press(first_line)
    first_line.fill("110 x 5")
    if viewport["is_phone"]:
        sub_lines.nth(1).tap()
    else:
        first_line.press("Tab")
    expect(card.get_by_test_id("sub-line-queued").first).to_be_visible()
    shot("00-retyped-offline")

    cell_entries = page.evaluate(
        "JSON.parse(localStorage.getItem('meso-log-queue') || '[]')"
        ".filter((i) => i.kind === 'cell')"
    )
    assert len(cell_entries) == 1, cell_entries
    assert cell_entries[0]["body"]["text"] == "110 x 5", cell_entries

    press(page.get_by_test_id("session-log"))
    expect(page.get_by_text(SAVED_OFFLINE_TEXT)).to_be_visible()

    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
    )
    page.wait_for_function("() => window.__e2eInflight === 0")
    shot("01-synced")

    cell = _squat_line_cell(delivered_plan)
    assert cell.text == "110 x 5"

    rows = list(
        LoggedSet.objects.filter(
            session_log__session=delivered_plan.session,
            session_log__athlete=delivered_plan.athlete,
            source_line=cell,
        )
    )
    assert len(rows) == 1, rows
    assert rows[0].load == "110"
    assert rows[0].reps == "5"

    log = SessionLog.objects.get(
        session=delivered_plan.session, athlete=delivered_plan.athlete
    )
    assert log.status == SessionLog.Status.DONE

    page.reload()
    card = _box_squat_card(page)
    expect(card.get_by_test_id("sub-line-input").first).to_have_value("110 x 5")
    shot("02-reloaded")
