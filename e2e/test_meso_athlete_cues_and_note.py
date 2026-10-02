"""Athlete journey: coach cues after the sets, and the session note (#524, #690).

The coach's cue for an exercise ("Brace hard before every rep", a coach-written
line 1) is read-only text AFTER the athlete's own lines, never an input; the
athlete's empty lines skip the line numbers a cue occupies and hint at the set's
own target. Below the exercises sits one "Notes for your coach" box that saves as
the athlete types, survives a reload, stays editable after Finish, and shows up
on the coach's results page. The offline tests cut the network under the same
note: it is written ahead to `localStorage["meso-log-queue"]`, says it is saved
offline, and lands on reconnect, alone or merged with a Finish queued after it.
"""

import re

import pytest
from django.urls import reverse
from playwright.sync_api import expect
from store_project.meso.models import Prescription
from store_project.meso.models import SessionLog
from store_project.meso.tests._helpers import sub_line

from e2e._coach_nav import assert_completion
from e2e._coach_nav import open_latest_results
from e2e._layout import assert_fits

pytestmark = pytest.mark.django_db

CUE = "Brace hard before every rep"
NOTE = "Left shoulder pinched on the last set.\nBack felt fine."
ADDENDUM = " Also: the bar felt heavy today."
SAVED_OFFLINE_NOTE = "Saved offline — will sync when you’re back."
SAVED_NOTE = "Saved ✓"
QUEUE_EMPTY_JS = (
    "() => JSON.parse(localStorage.getItem('meso-log-queue') || '[]').length === 0"
)

# Counts fetches still in flight (as in test_meso_athlete_offline.py), so a test
# can wait for what a reconnect sends to have landed before it reads back.
INFLIGHT_JS = """(() => {
  const fetch = window.fetch;
  window.__e2eInflight = 0;
  window.fetch = (...args) => {
    window.__e2eInflight += 1;
    return fetch(...args).finally(() => { window.__e2eInflight -= 1; });
  };
})();"""


@pytest.fixture
def cued_plan(delivered_plan):
    """`delivered_plan` with a coach-written cue on line 1 of the Box Squat."""
    sub_line(delivered_plan.squat, CUE, line=1)  # athlete_authored defaults False
    return delivered_plan


def _open(page, plan):
    page.goto(reverse("meso:athlete_session", kwargs={"pk": plan.session.pk}))
    expect(page.get_by_role("heading", name="Lower")).to_be_visible()
    expect(_squat(page).get_by_test_id("sub-line-input").first).to_be_visible()


def _squat(page):
    return page.get_by_test_id("exercise-card").filter(has_text="Box Squat")


def _prescribed(page):
    """N from the "0 of N sets logged" label — the fixture decides it."""
    text = page.get_by_test_id("set-progress").inner_text().strip()
    match = re.fullmatch(r"0 of (\d+) sets logged", text)
    assert match, text
    return int(match.group(1))


def _blur(page, press):
    """Move focus off whatever is being typed in, onto plain page text."""
    press(page.get_by_role("heading", name="Lower"))


def _log(plan):
    return SessionLog.objects.get(session=plan.session, athlete=plan.athlete)


def _note_status(page):
    return page.get_by_test_id("session-note-status")


def test_cue_after_sets_three_sets_and_note_reach_the_coach(
    page, viewport, shot, press, login, new_page, cued_plan
):
    login(cued_plan.athlete)
    _open(page, cued_plan)
    total = _prescribed(page)
    assert total >= 3
    squat = _squat(page)

    # --- the cue: read-only, AFTER the athlete's lines, never an input ---
    cue = squat.get_by_test_id("coach-cue")
    expect(cue).to_have_count(1)
    expect(cue).to_be_visible()
    assert cue.inner_text().strip() == CUE
    assert cue.evaluate("(el) => el.tagName") != "INPUT"
    expect(cue.locator("input, textarea")).to_have_count(0)
    expect(squat.get_by_test_id("coach-cues")).to_contain_text("from your coach")
    inputs = squat.get_by_test_id("sub-line-input")
    expect(inputs).to_have_count(3)  # three prescribed sets, three empty lines
    for i in range(3):
        expect(inputs.nth(i)).to_have_value("")
        assert inputs.nth(i).get_attribute("value") in (None, "")
    # No input is the cue's text anywhere in the card.
    assert not squat.evaluate(
        "(card, cue) => [...card.querySelectorAll('input')].some((i) => i.value === cue)",
        CUE,
    )
    last_input_box = inputs.nth(2).bounding_box()
    cue_box = cue.bounding_box()
    assert cue_box["y"] >= last_input_box["y"] + last_input_box["height"] - 1, (
        f"the cue ({cue_box}) is not below the last empty line ({last_input_box})"
    )
    # And the note box sits below every exercise, above Finish.
    note_box = page.get_by_test_id("session-note").bounding_box()
    finish_box = page.get_by_test_id("session-finish").bounding_box()
    last_card = page.get_by_test_id("exercise-card").last.bounding_box()
    assert note_box["y"] >= last_card["y"] + last_card["height"] - 1
    assert finish_box["y"] >= note_box["y"] + note_box["height"] - 1
    shot("01-open")
    assert_fits(page)

    # --- three sets typed into the empty lines ---
    progress = page.get_by_test_id("set-progress")
    for i, text in enumerate(["70 x 6", "70 x 6", "70 x 5"]):
        line = inputs.nth(i)
        press(line)
        line.fill(text)
        _blur(page, press)
        expect(progress).to_have_text(f"{i + 1} of {total} sets logged")
    shot("02-three-sets")

    # None of them landed on line 1: that is the coach's, untouched.
    cue_cell = Prescription.objects.get(
        exercise_slot=cued_plan.squat.exercise_slot, week=cued_plan.week, line=1
    )
    assert cue_cell.text == CUE
    assert cue_cell.athlete_authored is False
    own = Prescription.objects.filter(
        exercise_slot=cued_plan.squat.exercise_slot,
        week=cued_plan.week,
        athlete_authored=True,
    )
    assert sorted(own.values_list("line", flat=True)) == [2, 3, 4]

    # --- the session note saves as typed, status unchanged ---
    note = page.get_by_test_id("session-note-input")
    press(note)
    note.fill(NOTE)
    expect(_note_status(page).get_by_text(SAVED_NOTE)).to_be_visible()  # debounced
    assert _log(cued_plan).notes == NOTE
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    assert _log(cued_plan).status != SessionLog.Status.DONE
    shot("03-note-saved")

    # --- close and reopen: the note, the cue and the lines all persist ---
    page.reload()
    squat = _squat(page)
    expect(page.get_by_test_id("session-note-input")).to_have_value(NOTE)
    expect(squat.get_by_test_id("coach-cue")).to_have_text(CUE)
    expect(squat.get_by_test_id("sub-line-input").first).to_have_value("70 x 6")
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"3 of {total} sets logged"
    )
    shot("04-reopened")
    assert_fits(page)

    # --- Finish: the note box stays, and stays editable ---
    press(page.get_by_test_id("session-finish"))
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    note = page.get_by_test_id("session-note-input")
    expect(note).to_be_visible()
    expect(note).to_be_editable()
    expect(note).to_have_value(NOTE)
    press(note)
    note.fill(NOTE + ADDENDUM)
    expect(_note_status(page).get_by_text(SAVED_NOTE)).to_be_visible()
    log = _log(cued_plan)
    assert log.notes == NOTE + ADDENDUM
    assert log.status == SessionLog.Status.DONE  # a note post is not a Finish
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    shot("05-finished-addendum")

    # --- the coach's results page ---
    coach_page = new_page(desktop=True)
    login(cued_plan.coach, on=coach_page)
    open_latest_results(coach_page)
    athlete_note = coach_page.get_by_test_id("athlete-note")
    expect(athlete_note).to_be_visible()
    text = " ".join(athlete_note.inner_text().split())
    assert " ".join((NOTE + ADDENDUM).split()) in text, text
    assert_completion(coach_page, round(3 / total * 100), f"3 of {total} sets logged")
    shot("06-coach-results", on=coach_page, viewport_id="desktop")


def test_typing_the_placeholder_logs_the_prescribed_set(
    page, viewport, shot, press, login, cued_plan
):
    login(cued_plan.athlete)
    _open(page, cued_plan)
    total = _prescribed(page)
    first = _squat(page).get_by_test_id("sub-line-input").first
    hint = first.get_attribute("placeholder")
    # "3 x 6, RPE 7, 70" -> the set as a line the parser accepts.
    assert hint == "70 x 6", hint
    press(first)
    first.fill(hint)
    _blur(page, press)
    expect(page.get_by_test_id("set-progress")).to_have_text(
        f"1 of {total} sets logged"
    )
    expect(
        first.locator("xpath=..").get_by_test_id("sub-line-warn")
    ).not_to_be_visible()
    shot("01-placeholder-typed")


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def _online_watcher(page):
    page.evaluate(
        "window.__e2eOnlineFired = false;"
        "window.addEventListener('online', () => { window.__e2eOnlineFired = true; });"
    )


def _reconnect(page, context):
    context.set_offline(False)
    page.wait_for_function("() => window.__e2eOnlineFired === true", timeout=5000)
    page.wait_for_function(QUEUE_EMPTY_JS)
    page.wait_for_function("() => window.__e2eInflight === 0")


def test_note_typed_offline_lands_on_reconnect(
    page, context, viewport, shot, press, login, cued_plan
):
    page.add_init_script(INFLIGHT_JS)
    login(cued_plan.athlete)
    _open(page, cued_plan)
    _online_watcher(page)
    context.set_offline(True)

    note = page.get_by_test_id("session-note-input")
    press(note)
    note.fill(NOTE)
    expect(_note_status(page).get_by_text(SAVED_OFFLINE_NOTE)).to_be_visible()
    # The status line for the SESSION (Finish footer) is not the note's.
    expect(
        page.locator(".meso-log-actions").get_by_text(SAVED_OFFLINE_NOTE)
    ).to_be_hidden()
    queue = page.evaluate("JSON.parse(localStorage.getItem('meso-log-queue') || '[]')")
    assert len(queue) == 1 and queue[0]["body"] == {"notes": NOTE}, queue
    assert (
        not SessionLog.objects.filter(
            session=cued_plan.session, athlete=cued_plan.athlete
        )
        .exclude(notes="")
        .exists()
    )
    shot("01-offline-note")

    _reconnect(page, context)
    expect(_note_status(page).get_by_text(SAVED_OFFLINE_NOTE)).to_be_hidden()
    assert _log(cued_plan).notes == NOTE
    assert _log(cued_plan).status != SessionLog.Status.DONE

    page.reload()
    expect(page.get_by_test_id("session-note-input")).to_have_value(NOTE)
    expect(page.get_by_test_id("session-status")).to_have_text("To do")
    shot("02-synced")


def test_note_and_finish_typed_offline_both_land_on_reconnect(
    page, context, viewport, shot, press, login, new_page, cued_plan
):
    page.add_init_script(INFLIGHT_JS)
    login(cued_plan.athlete)
    _open(page, cued_plan)
    total = _prescribed(page)
    _online_watcher(page)
    context.set_offline(True)

    note = page.get_by_test_id("session-note-input")
    press(note)
    note.fill(NOTE)
    expect(_note_status(page).get_by_text(SAVED_OFFLINE_NOTE)).to_be_visible()

    press(page.get_by_test_id("session-finish"))
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    expect(
        page.locator(".meso-log-actions").get_by_text(SAVED_OFFLINE_NOTE)
    ).to_be_visible()
    # One queued log entry carrying BOTH keys: neither erased the other.
    queue = page.evaluate("JSON.parse(localStorage.getItem('meso-log-queue') || '[]')")
    assert len(queue) == 1, queue
    assert queue[0]["body"] == {"notes": NOTE, "status": "done"}, queue
    assert (
        not SessionLog.objects.filter(
            session=cued_plan.session, athlete=cued_plan.athlete
        ).exists()
        or _log(cued_plan).notes == ""
    )
    shot("01-offline-note-and-finish")

    _reconnect(page, context)
    log = _log(cued_plan)
    assert log.status == SessionLog.Status.DONE
    assert log.notes == NOTE
    expect(page.get_by_test_id("session-status")).to_have_text("Logged")
    expect(page.get_by_test_id("session-note-input")).to_have_value(NOTE)

    coach_page = new_page(desktop=True)
    login(cued_plan.coach, on=coach_page)
    open_latest_results(coach_page)
    athlete_note = coach_page.get_by_test_id("athlete-note")
    expect(athlete_note).to_be_visible()
    assert " ".join(NOTE.split()) in " ".join(athlete_note.inner_text().split())
    assert_completion(coach_page, 0, f"0 of {total} sets logged")
    shot("02-coach-results", on=coach_page, viewport_id="desktop")
